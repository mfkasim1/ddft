from abc import abstractmethod
from typing import List, Union, Tuple
import torch
import xitorch as xt

class BaseQCCalc(xt.EditableModule):
    @abstractmethod
    def run(self, **kwargs):
        """
        Run the calculation.
        Note that this method can be invoked several times for one object to
        try for various self-consistent options to reach convergence.
        """
        pass

    @abstractmethod
    def energy(self) -> torch.Tensor:
        """
        Obtain the energy of the system.
        """
        pass

    @abstractmethod
    def aodm(self) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        Returns the density matrix in atomic orbital. For polarized case, it
        returns a tuple of 2 tensors representing the density matrices for
        spin-up and spin-down.
        """
        # return: (nao, nao)
        pass

    @abstractmethod
    def getparamnames(self, methodname: str, prefix: str = "") -> List[str]:
        pass
