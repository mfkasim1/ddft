from typing import Optional, Mapping, Any, Tuple
import torch
import xitorch as xt
import xitorch.linalg
import xitorch.optimize
from dqc.system.base_system import BaseSystem
from dqc.qccalc.base_qccalc import BaseQCCalc
from dqc.xc.base_xc import BaseXC
from dqc.api.getxc import get_xc

class RKS(BaseQCCalc):
    """
    Performing Restricted Kohn-Sham DFT calculation.

    Arguments
    ---------
    system: BaseSystem
        The system to be calculated.
    xc: str
        The exchange-correlation potential and energy to be used.
    vext: torch.Tensor or None
        The external potential applied to the system. It must have the shape of
        ``(*BV, system.get_grid().shape[-2])``
    """

    def __init__(self, system: BaseSystem, xc: str,
                 vext: Optional[torch.Tensor] = None):
        # get the xc object
        self.xc: BaseXC = get_xc(xc, polarized=False)
        self.system = system

        # build and setup basis and grid
        self.system.setup_grid()
        self.hamilton = system.get_hamiltonian()
        self.hamilton.build()
        self.hamilton.setup_grid(system.get_grid(), self.xc.family)

        # get the orbital info
        self.orb_weight = system.get_orbweight()  # (norb,)
        self.norb = self.orb_weight.shape[-1]

        # set up the vext linear operator
        self.knvext_linop = self.hamilton.get_kinnucl()  # kinetic, nuclear, and external potential
        if vext is not None:
            assert vext.shape[-1] == system.get_grid().shape[-2]
            self.knvext_linop = self.knvext_linop + self.hamilton.get_vext(vext)

        self.overlap_linop = self.hamilton.get_overlap()  # overlap linear operator

        # misc info
        self.dtype = self.knvext_linop.dtype
        self.device = self.knvext_linop.device
        self.has_run = True

    def run(self, dm0: Optional[torch.Tensor] = None,  # type: ignore
            eigen_options: Optional[Mapping[str, Any]] = None,
            fwd_options: Optional[Mapping[str, Any]] = None,
            bck_options: Optional[Mapping[str, Any]] = None) -> BaseQCCalc:

        # setup the default options
        if eigen_options is None:
            eigen_options = {}
        if fwd_options is None:
            fwd_options = {
                "method": "broyden1",
                "alpha": -0.5,
                "maxiter": 50,
            }
        if bck_options is None:
            bck_options = {}  # let xitorch decide

        # save the eigen_options for use in diagonalization
        self.eigen_options = eigen_options

        # set up the initial self-consistent param guess
        if dm0 is None:
            dm0 = torch.zeros(self.knvext_linop.shape, dtype=self.dtype,
                              device=self.device)
        scp0 = self.__dm2scp(dm0)

        # do the self-consistent iteration
        scp = xitorch.optimize.equilibrium(
            fcn=self.__scp2scp,
            y0=scp0,
            bck_options={**bck_options},
            **fwd_options)

        # post-process parameters
        self._dm = self.__scp2dm(scp)
        self.has_run = True
        return self

    def energy(self) -> torch.Tensor:
        # calculate the total energy from the diagonalization
        fock = self.__dm2fock(self._dm)
        # eivals: (..., norb), eivecs: (..., norb, nao)
        eivals, eivecs = self.__diagonalize_fock(fock)
        eivals_tot = torch.sum(eivals * self.orb_weight, dim=-1)

        # get the energy from xc
        exc = self.hamilton.get_exc(self.xc, self._dm)

        # get the energy from xc potential
        vxc = self.hamilton.get_vxc(self.xc, self._dm)
        e_vxc = torch.einsum("...rc,r,...rc->...", vxc.mm(eivecs), self.orb_weight, eivecs)

        # get the energy from electron repulsion
        elrep = self.hamilton.get_elrep(self._dm)
        e_elrep = 0.5 * torch.einsum("...rc,r,...rc->...", elrep.mm(eivecs), self.orb_weight, eivecs)

        # compute the total energy
        e_tot = eivals_tot + (exc - vxc) - e_elrep + self.system.get_nuclei_energy()
        return e_tot

    def aodm(self) -> torch.Tensor:
        return self._dm

    def __dm2fock(self, dm: torch.Tensor) -> xt.LinearOperator:
        # construct the fock matrix from the density matrix
        ks_linop = self.__get_ks_linop(dm)  # (..., nao, nao)
        return self.knvext_linop + ks_linop

    def __fock2dm(self, fock: xt.LinearOperator) -> torch.Tensor:
        # diagonalize the fock matrix and obtain the density matrix
        eigvals, eigvecs = self.__diagonalize_fock(fock)
        dm = self.hamilton.ao_orb2dm(eigvecs, self.orb_weight)
        return dm

    def __get_ks_linop(self, dm: torch.Tensor) -> xt.LinearOperator:
        elrep = self.hamilton.get_elrep(dm)  # (..., nao, nao)
        vxc = self.hamilton.get_vxc(self.xc, dm)
        return elrep + vxc

    def __diagonalize_fock(self, fock: xt.LinearOperator) -> Tuple[torch.Tensor, torch.Tensor]:
        return xitorch.linalg.lsymeig(
            A=fock,
            neig=self.norb,
            M=self.overlap_linop,
            **self.eigen_options)

    ######### self-consistent-param related #########
    # the functions below are created so that it is easy to change which
    # parameters is involved in the self-consistent iterations

    def __dm2scp(self, dm: torch.Tensor) -> torch.Tensor:
        # scp is the fock matrix
        return self.__dm2fock(dm).fullmatrix()

    def __scp2dm(self, scp: torch.Tensor) -> torch.Tensor:
        fock = xt.LinearOperator.m(scp, is_hermitian=True)
        return self.__fock2dm(fock)

    def __scp2scp(self, scp: torch.Tensor) -> torch.Tensor:
        dm = self.__scp2dm(scp)
        return self.__dm2scp(dm)