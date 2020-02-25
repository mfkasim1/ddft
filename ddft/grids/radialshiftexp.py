from abc import abstractmethod
import torch
import numpy as np
from numpy.polynomial.legendre import leggauss, legvander
from ddft.grids.base_grid import BaseGrid, BaseRadialGrid
from ddft.utils.legendre import legint

class RadialShiftExp(BaseRadialGrid):
    def __init__(self, rmin, rmax, nr, dtype=torch.float, device=torch.device('cpu')):
        logr = torch.linspace(np.log(rmin), np.log(rmax), nr).to(dtype).to(device)
        unshifted_rgrid = torch.exp(logr)
        self._boxshape = (nr,)
        self.rmin = rmin
        self.rs = unshifted_rgrid - self.rmin
        self._rgrid = self.rs.unsqueeze(1) # (nr, 1)
        self.dlogr = logr[1] - logr[0]

        # integration elements
        self._scaling = (self.rs + self.rmin)
        self._dr = self._scaling * self.dlogr
        self._dvolume = 4 * np.pi * self.rs*self.rs * self._dr

    def get_dvolume(self):
        return self._dvolume

    def solve_poisson(self, f):
        # f: (nbatch, nr)
        # the expression below is used to make the operator symmetric
        eps = 1e-10
        intgn1 = f * self.rs * self.rs
        int1 = self.antiderivative(intgn1, dim=-1, zeroat="left")
        intgn2 = int1 / (self.rs * self.rs + eps)
        # this form of cumsum is the transpose of torch.cumsum
        int2 = self.antiderivative(intgn2, dim=-1, zeroat="right")
        return int2

    @property
    def rgrid(self):
        return self._rgrid

    @property
    def boxshape(self):
        return self._boxshape

    def antiderivative(self, intgn, dim=-1, zeroat="left"):
        intgn = intgn * self._dr
        if zeroat == "left":
            return torch.cumsum(intgn, dim=dim)
        elif zeroat == "right":
            return -torch.cumsum(intgn.flip(dims=[dim]), dim=dim).flip(dims=[dim])

class LegendreRadialShiftExp(BaseRadialGrid):
    def __init__(self, rmin, rmax, nr, dtype=torch.float, device=torch.device('cpu')):
        self._boxshape = (nr,)
        self.rmin = rmin

        # setup the legendre points
        logrmin = torch.tensor(np.log(rmin)).to(dtype).to(device)
        logrmax = torch.tensor(np.log(rmax)).to(dtype).to(device)
        self.logrmm = logrmax - logrmin
        xleggauss, wleggauss = leggauss(nr)
        self.xleggauss = torch.tensor(xleggauss, dtype=dtype, device=device)
        self.wleggauss = torch.tensor(wleggauss, dtype=dtype, device=device)
        self.rs = torch.exp((self.xleggauss+1)*0.5 * (logrmax - logrmin) + logrmin) - rmin
        self._rgrid = self.rs.unsqueeze(-1)

        # integration elements
        self._scaling = (self.rs+self.rmin) * self.logrmm * 0.5 # dr/dg
        self._dr = self._scaling * self.wleggauss
        self._dvolume = (4*np.pi*self.rs*self.rs) * self._dr

        # legendre basis (from tinydft/tinygrid.py)
        basis = legvander(xleggauss, nr-1) # (nr, nr)
        # U, S, Vt = np.linalg.svd(basis)
        # inv_basis = np.einsum('ji,j,kj->ik', Vt, 1 / S, U) # (nr, nr)
        self.basis = torch.tensor(basis.T).to(dtype).to(device)
        self.inv_basis = self.basis.inverse()
        # self.inv_basis = torch.tensor(inv_basis.T).to(dtype).to(device)

    def get_dvolume(self):
        return self._dvolume

    def solve_poisson(self, f):
        # f: (nbatch, nr)
        # the expression below is used to make the operator symmetric
        eps = 1e-10
        intgn1 = f * self.rs * self.rs# * self._scaling
        int1 = self.antiderivative(intgn1, dim=-1, zeroat="left")

        intgn2 = int1 / (self.rs * self.rs + eps)# * self._scaling
        # this form of cumsum is the transpose of torch.cumsum
        int2 = self.antiderivative(intgn2, dim=-1, zeroat="right")
        return int2

    @property
    def rgrid(self):
        return self._rgrid

    @property
    def boxshape(self):
        return self._boxshape

    def antiderivative(self, intgn, dim=-1, zeroat="left"):
        # intgn: (nbatch, nr)
        intgn = intgn * self._scaling
        coeff = torch.matmul(intgn, self.inv_basis) # (nbatch, nr)
        intcoeff = legint(coeff, dim=dim, zeroat=zeroat)[:,:-1] # (nbatch, nr)
        res = torch.matmul(intcoeff, self.basis) # (nbatch, nr)
        return res
