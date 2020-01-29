import torch
from ddft.utils.misc import set_default_option
from ddft.maths.eig import eig
from ddft.utils.ortho import orthogonalize, biorthogonalize

"""
This file contains methods to obtain eigenpairs of a linear transformation
    which is a subclass of ddft.modules.base_linear.BaseLinearModule
"""

class eigendecomp(torch.autograd.Function):
    @staticmethod
    def forward(ctx, A, neig, options, *params):
        config = set_default_option({
            "method": "davidson",
        }, options)

        with torch.enable_grad():
            method = config["method"].lower()
            if method == "davidson":
                evals, evecs = davidson(A, neig, params, **options)
            elif method == "lanczos":
                evals, evecs = lanczos(A, neig, params, **options)
            elif method == "exacteig":
                evals, evecs = exacteig(A, neig, params, **options)
            else:
                raise RuntimeError("Unknown eigen decomposition method: %s" % config["method"])

        # save for the backward
        ctx.evals = evals # (nbatch, neig)
        ctx.evecs = evecs # (nbatch, na, neig)
        ctx.neig = neig
        ctx.params = params
        ctx.A = A
        return evals.clone(), evecs.clone()

    @staticmethod
    def backward(ctx, grad_evals, grad_evecs):
        grad_params = torch.autograd.grad((ctx.evals, ctx.evecs),
            ctx.params, grad_outputs=(grad_evals, grad_evecs),
            retain_graph=True)
        return (None, None, None, *grad_params)

        # # make the diagonal only A
        # V = ctx.evecs.clone().detach() # (nbatch, na, neig)
        # VT = V.transpose(-2,-1)
        # with torch.enable_grad():
        #     AV = ctx.A(V, *ctx.params) # (nbatch, na, neig)
        #     VTAV = torch.bmm(VT, AV) # (nbatch, neig, neig)
        #
        #     # evals: (nbatch, neig)
        #     # evecsH: (nbatch, neig, neig)
        #     evals, evecsH = torch.symeig(VTAV, eigenvectors=True)
        #     # evecs = torch.bmm(V, evecsH)
        #     # print(evecs)
        #     # print(ctx.evecs)
        #     # print(evecs - ctx.evecs)
        #
        # grad_evecsH = torch.bmm(VT, grad_evecs)
        #
        # # contribution from evals and evecs
        # # grad_params = torch.autograd.grad((evals, evecs), params,
        # #     grad_outputs=(grad_evals, grad_evecs),
        # #     create_graph=torch.is_grad_enabled())
        # grad_params = torch.autograd.grad((evals, evecsH), ctx.params,
        #     grad_outputs=(grad_evals, grad_evecsH),
        #     create_graph=torch.is_grad_enabled())
        #
        # return (None, None, None, *grad_params)

def lanczos(A, neig, params, **options):
    """
    Lanczos iterative method to obtain the `neig` lowest eigenpairs.
    This function is written so that the backpropagation can be done.

    Arguments
    ---------
    * A: BaseLinearModule instance
        The linear module object on which the eigenpairs are constructed.
    * neig: int
        The number of eigenpairs to be retrieved.
    * params: list of differentiable torch.tensor
        List of differentiable torch.tensor to be put to A.forward(x,*params).
        Each of params must have shape of (nbatch,...)
    * **options:
        Iterative algorithm options.

    Returns
    -------
    * eigvals: torch.tensor (nbatch, neig)
    * eigvecs: torch.tensor (nbatch, na, neig)
        The `neig` smallest eigenpairs
    """
    config = set_default_option({
        "max_niter": 120,
        "min_eps": 1e-6,
        "verbose": False,
        "v_init": "randn",
    }, options)
    verbose = config["verbose"]
    min_eps = config["min_eps"]

    # get the shape of the transformation
    na = _check_and_get_shape(A)
    nbatch = params[0].shape[0]
    dtype, device = _get_dtype_device(params, A)

    # set up the initial Krylov space
    V = _set_initial_v(config["v_init"].lower(), nbatch, na, 1) # (nbatch,na,1)
    V = V.to(dtype).to(device)

    prev_eigvals = None
    stop_reason = "max_niter"
    for i in range(config["max_niter"]):
        v = V[:,:,i] # (nbatch, na)
        Av = A(v, *params) # (nbatch, na)

        # construct the Krylov space
        V = torch.cat((V, Av.unsqueeze(-1)), dim=-1) # (nbatch, na, i+1)

        # orthogonalize
        Q, R = torch.qr(V) # Q: (nbatch, na, i+1)
        V = Q

        AV = A(V, *params) # (nbatch, na, i+1)
        T = torch.bmm(V.transpose(-2,-1), AV) # (nbatch, i+1, i+1)

        # check convergence
        if i+1 < neig: continue
        eigvalT, eigvecT = torch.symeig(T, eigenvectors=True) # val: (nbatch, i+1)
        eigvecA = torch.bmm(V, eigvecT) # (nbatch, na, i+1)
        if prev_eigvals is not None:
            dev = (eigvalT[:,:neig].data - prev_eigvals).abs().max()
            if verbose:
                print("Iter %3d (guess size: %d): %.3e" % (i, eigvecA.shape[-1], dev))
            if dev < min_eps:
                stop_reason = "min_eps"
                break
        prev_eigvals = eigvalT[:,:neig]

    eigvals = eigvalT[:,:neig]
    eigvecs = eigvecA[:,:,:neig]
    return eigvals, eigvecs

def davidson(A, neig, params, **options):
    """
    Iterative methods to obtain the `neig` lowest eigenvalues and eigenvectors.
    This function is written so that the backpropagation can be done.

    Arguments
    ---------
    * A: BaseLinearModule instance
        The linear module object on which the eigenpairs are constructed.
    * neig: int
        The number of eigenpairs to be retrieved.
    * params: list of differentiable torch.tensor
        List of differentiable torch.tensor to be put to A.forward(x,*params).
        Each of params must have shape of (nbatch,...)
    * **options:
        Iterative algorithm options.

    Returns
    -------
    * eigvals: torch.tensor (nbatch, neig)
    * eigvecs: torch.tensor (nbatch, na, neig)
        The `neig` smallest eigenpairs
    """
    config = set_default_option({
        "max_niter": 120,
        "nguess": neig+1,
        "min_eps": 1e-6,
        "verbose": False,
        "eps_cond": 1e-6,
        "v_init": "randn",
        "max_addition": neig,
    }, options)

    # get some of the options
    nguess = config["nguess"]
    max_niter = config["max_niter"]
    min_eps = config["min_eps"]
    verbose = config["verbose"]
    eps_cond = config["eps_cond"]
    max_addition = config["max_addition"]

    # get the shape of the transformation
    na = _check_and_get_shape(A)
    nbatch = params[0].shape[0]
    dtype, device = _get_dtype_device(params, A)

    # set up the initial guess
    V = _set_initial_v(config["v_init"].lower(), nbatch, na, nguess) # (nbatch,na,nguess)
    V = V.to(dtype).to(device)
    dA = A.diag(*params) # (nbatch, na)

    prev_eigvals = None
    stop_reason = "max_niter"
    for m in range(nguess, max_niter, nguess):
        VT = V.transpose(-2,-1)
        # print(torch.bmm(VT, V))
        # Can be optimized by saving AV from the previous iteration and only
        # operate AV for the new V. This works because the old V has already
        # been orthogonalized, so it will stay the same
        AV = A(V, *params) # (nbatch, na, nguess)
        T = torch.bmm(VT, AV) # (nbatch, nguess, nguess)

        # eigvals are sorted from the lowest
        # eval: (nbatch, nguess), evec: (nbatch, nguess, nguess)
        eigvalT, eigvecT = torch.symeig(T, eigenvectors=True)
        eigvecA = torch.bmm(V, eigvecT) # (nbatch, na, nguess)

        # check the convergence
        if prev_eigvals is not None:
            dev = (eigvalT[:,:neig].data - prev_eigvals).abs().max()
            if verbose:
                print("Iter %3d (guess size: %d): %.3e" % (m, eigvecA.shape[-1], dev))
            if dev < min_eps:
                stop_reason = "min_eps"
                break

        # stop if V has become a full-rank matrix
        if V.shape[-1] == na:
            stop_reason = "full_rank"
            break

        # calculate the parameters for the next iteration
        prev_eigvals = eigvalT[:,:neig].data

        nj = eigvalT.shape[1]
        ritz_list = []
        nadd = min(nj, max_addition)
        for i in range(nadd):
            # precondition the inverse diagonal
            finv = dA - eigvalT[:,i:i+1]
            finv[finv.abs() < eps_cond] = eps_cond
            f = 1. / finv # (nbatch, na)

            AVphi = A(eigvecA[:,:,i], *params) # (nbatch, na)
            lmbdaVphi = eigvalT[:,i:i+1] * eigvecA[:,:,i] # (nbatch, na)
            r = f * (AVphi - lmbdaVphi) # (nbatch, na)
            r = r.unsqueeze(-1)
            ritz_list.append(r)

        # add the ritz vectors to the guess vectors
        ritz = torch.cat(ritz_list, dim=-1)
        ritz = ritz / ritz.norm(dim=1, keepdim=True) # (nbatch, na, nguess)
        Va = torch.cat((V, ritz), dim=-1)
        if V.shape[-1] > na:
            V = V[:,:,-na:]

        # R^{-T} is needed for the backpropagation, so small det(R) will cause
        # numerical instability. So we need to choose ritz that are
        # perpendicular to the current columns of V

        # orthogonalize the new columns of V
        Q, R = torch.qr(Va) # V: (nbatch, na, nguess), R: (nbatch, nguess, nguess)
        V = Q

    eigvals = eigvalT[:,:neig]
    eigvecs = eigvecA[:,:,:neig]
    return eigvals, eigvecs

def exacteig(A, neig, params, **options):
    """
    The exact method to obtain the `neig` lowest eigenvalues and eigenvectors.
    This function is written so that the backpropagation can be done.

    Arguments
    ---------
    * A: BaseLinearModule instance
        The linear module object on which the eigenpairs are constructed.
    * neig: int
        The number of eigenpairs to be retrieved.
    * params: list of differentiable torch.tensor
        List of differentiable torch.tensor to be put to A.forward(x,*params).
        Each of params must have shape of (nbatch,...)
    * **options:
        The algorithm options.

    Returns
    -------
    * eigvals: torch.tensor (nbatch, neig)
    * eigvecs: torch.tensor (nbatch, na, neig)
        The `neig` smallest eigenpairs
    """
    na = _check_and_get_shape(A)
    nbatch = params[0].shape[0]
    dtype, device = _get_dtype_device(params, A)
    V = torch.eye(na).unsqueeze(0).expand(nbatch,-1,-1).to(dtype).to(device)

    # obtain the full matrix of A
    Amatrix = A(V, *params)
    evals, evecs = torch.symeig(Amatrix, eigenvectors=True)

    return evals[:,:neig], evecs[:,:,:neig]

def _get_dtype_device(params, A):
    A_params = list(A.parameters())
    if len(A_params) == 0:
        p = params[0]
    else:
        p = A_params[0]
    dtype = p.dtype
    device = p.device
    return dtype, device

def _check_and_get_shape(A):
    na, nc = A.shape
    if na != nc:
        raise TypeError("The linear transformation of davidson method must be a square matrix")
    return na

def _set_initial_v(vinit_type, nbatch, na, nguess):
    torch.manual_seed(12421)
    ortho = False
    if vinit_type == "eye":
        V = torch.eye(na, nguess).unsqueeze(0).repeat(nbatch,1,1)
        ortho = True
    elif vinit_type == "randn":
        V = torch.randn(nbatch, na, nguess)
    elif vinit_type == "random" or vinit_type == "rand":
        V = torch.rand(nbatch, na, nguess)
    else:
        raise ValueError("Unknown v_init type: %s" % vinit_type)

    # orthogonalize V
    if not ortho:
        V, R = torch.qr(V)
    return V

if __name__ == "__main__":
    from ddft.utils.fd import finite_differences

    # generate the matrix
    na = 20
    dtype = torch.float64
    A1 = (torch.rand((1,na,na))*0.1).to(dtype).requires_grad_(True)
    diag = torch.arange(na, dtype=dtype).unsqueeze(0).requires_grad_(True)

    class Acls:
        def __call__(self, x, A1, diag):
            xndim = x.ndim
            if xndim == 2:
                x = x.unsqueeze(-1)

            Amatrix = (A1 + A1.transpose(-2,-1))
            A = Amatrix + diag.diag_embed(dim1=-2, dim2=-1)
            y = torch.bmm(A, x)

            if xndim == 2:
                y = y.squeeze(-1)
            return y

        def parameters(self):
            return []

        @property
        def shape(self):
            return (na,na)

        def diag(self, A1, dg):
            Amatrix = (A1 + A1.transpose(-2,-1))
            A = Amatrix + diag.diag_embed(dim1=-2, dim2=-1)
            return A.diagonal(dim1=-2, dim2=-1)

    def getloss(A1, diag):
        A = Acls()
        neig = 1
        options = {
            "method": "davidson",
            "verbose": True,
            "v_init": "rand",
        }
        evals, evecs = eigendecomp.apply(A, neig, options, A1, diag)
        loss = 0
        # loss = loss + (evals**2).sum()
        loss = loss + (evecs**4).sum()
        return loss

    loss = getloss(A1, diag)
    loss.backward()
    print("Backward done")
    Agrad = A1.grad.data
    dgrad = diag.grad.data

    Afd = finite_differences(getloss, (A1, diag,), 0, eps=1e-5)
    dfd = finite_differences(getloss, (A1, diag,), 1, eps=1e-5)
    print("Finite differences done")

    print(Agrad)
    print(Afd)
    print(Agrad/Afd)

    print(dgrad)
    print(dfd)
    print(dgrad/dfd)
