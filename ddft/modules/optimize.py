import torch
import xitorch as xt
from ddft.utils.misc import set_default_option

class OptimizationModule(torch.nn.Module):
    """
    OptimizationModule finds the optimum solution from a given model.

        z* = min_x model(*x, *y)
        x* = argmin_x model(*x, *y)

    The model should take the optimized parameters, x, and the external
    parameters, y.
    The separation of x and y is specified by `optimized_nparams`.
    The variables `x` and `y` must be a list of tensor.
    When the model is wrapped with an OptimizationModule, the new module
    takes `y` as the input and produce `z*` (and `x*`) as the outputs.

    This module cannot propagate the gradient from `x*`.

    Initialization arguments
    ------------------------
    * model: torch.nn.Module
        The torch module where the forward is called to get the loss function.
    * optimized_nparams: int
        Indicating how many initial arguments to be specified as the optimized
        parameters.
    * minimize: bool
        If True, perform minimization. Otherwise, perform maximization.
    * optimize_model: bool
        If True, the model's parameters are also optimized, but not returned
        as the output. If model has method "optimizing_parameters", then it will
        use the parameters from "optimizing_parameters". Otherwise, it will use
        the parameters from "parameters"
    * return_arg: bool
        If True, then return (z*, x*) in the forward. Otherwise, only return z*
    * forward_options: dict
        The option for optimization algorithm.
    * backward_options: dict
        (Has no effect just yet)
    """
    def __init__(self, model, optimized_nparams, minimize=True,
                 optimize_model=False, return_arg=False,
                 forward_options={}, backward_options={}):
        super(OptimizationModule, self).__init__()
        self.model = model
        self.optimize_model = optimize_model
        self.fwd_options = forward_options
        self.bck_options = backward_options
        self.multiplier = 1.0 if minimize else -1.0
        self.optimized_nparams = optimized_nparams
        self.return_arg = return_arg

    def forward(self, *params):
        # split into optimized parameters and external parameters
        x0 = params[:self.optimized_nparams]
        yparams = params[self.optimized_nparams:]

        results = _ForwardOpt.apply(self.model, self.multiplier, x0,
            self.fwd_options, yparams, self.optimize_model)
        zopt = results[0]
        xopt = results[1:]
        if self.training:
            for p in yparams: p.requires_grad_()
            # zopt2 = self.model(xopt, yparams)
            # res = _BackwardOpt.apply(zopt2, self.bck_options, xopt)
            # zopt = res[0]
            # xopt = res[1:]
            zopt = self.model(*xopt, *yparams)
        if self.return_arg:
            return zopt, xopt
        else:
            return zopt

class _ForwardOpt(torch.autograd.Function):
    @staticmethod
    def forward(ctx, model, mult, x0, fwd_options, yparams, optimize_model):
        # set default options
        config = set_default_option({
            "max_niter": 100,
            "min_eps": 1e-6,
            "verbose": False,
            "lr": 1e-2,
            "method": "lbfgs",
        }, fwd_options)

        verbose = config["verbose"]

        # get the algorithm class
        method = config["method"].lower()
        if method == "lbfgs":
            opt_cls = torch.optim.LBFGS
            opt_kwds = ["lr"]
        elif method == "sgd":
            opt_cls = torch.optim.SGD
            opt_kwds = ["lr", "momentum", "dampening", "weight_decay", "nesterov"]

        opt_kwargs = {x:config[x] for x in opt_kwds if x in config}

        with torch.enable_grad():
            x = [p.detach().clone().requires_grad_() for p in x0]
            y = [p.detach().clone() for p in yparams]

            if optimize_model:
                if hasattr(model, "optimizing_parameters"):
                    mparams = list(model.optimizing_parameters())
                else:
                    mparams = list(model.parameters())
                params = x + mparams
            else:
                params = x
            if len(params) == 0:
                raise RuntimeError("There is no parameters to be optimized in the OptimizationModule")

            opt = opt_cls(params, **opt_kwargs)
            for i in range(config["max_niter"]):
                def closure():
                    opt.zero_grad()
                    z = model(*x, *y) * mult
                    z.backward()
                    if verbose and i%10 == 0:
                        print("Iter %3d: %.3e" % (i, z))
                    return z

                opt.step(closure)

        # reset all the gradients
        opt.zero_grad()
        for p in model.parameters():
            p.grad.zero_()

        xopt = x
        zopt = model(*xopt, *y)
        res = (zopt, *xopt)
        return res

    @staticmethod
    def backward(ctx, grad_zopt, *grad_xopt):
        return (None, None, None, None, None, None)

class _BackwardOpt(torch.autograd.Function):
    @staticmethod
    def forward(ctx, fmodel, bck_options, xopt):
        ctx.fmodel = fmodel
        ctx.bck_options = bck_options
        ctx.xopt = xopt
        res = (fmodel, *xopt)
        return res

    @staticmethod
    def backward(ctx, grad_zopt, *grad_xopt):
        grad_fmodel = grad_zopt
        allzeros = True
        for gx in grad_xopt:
            if not torch.allclose(gx, gx*0):
                allzeros = False
                break
        if allzeros:
            return (grad_fmodel, None, None)
        else:
            raise RuntimeError("Unimplemented gradient contribution from the argmin")
