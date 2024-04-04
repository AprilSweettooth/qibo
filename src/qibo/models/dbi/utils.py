import math
from copy import deepcopy
from itertools import combinations, product
from typing import Optional

import hyperopt
import numpy as np

from qibo import symbols
from qibo.backends import _check_backend
from qibo.hamiltonians import SymbolicHamiltonian
from qibo.models.dbi.double_bracket import (
    DoubleBracketGeneratorType,
    DoubleBracketIteration,
    DoubleBracketScheduling,
)


def generate_Z_operators(nqubits: int, backend=None):
    """Generate a dictionary containing 1) all possible products of Pauli Z operators for L = n_qubits and 2) their respective names.
    Return: Dictionary with operator names (str) as keys and operators (np.array) as values

     Example:
        .. testcode::

            from qibo.models.dbi.utils import generate_Z_operators
            from qibo.models.dbi.double_bracket import DoubleBracketIteration
            from qibo.quantum_info import random_hermitian
            from qibo.hamiltonians import Hamiltonian
            import numpy as np

            nqubits = 4
            h0 = random_hermitian(2**nqubits)
            dbi = DoubleBracketIteration(Hamiltonian(nqubits=nqubits, matrix=h0))
            generate_Z = generate_Z_operators(nqubits)
            Z_ops = list(generate_Z.values())

            delta_h0 = dbi.diagonal_h_matrix
            dephasing_channel = (sum([Z_op @ h0 @ Z_op for Z_op in Z_ops])+h0)/2**nqubits
            norm_diff = np.linalg.norm(delta_h0 - dephasing_channel)
    """

    backend = _check_backend(backend)
    # list of tuples, e.g. ('Z','I','Z')
    combination_strings = product("ZI", repeat=nqubits)
    output_dict = {}

    for zi_string_combination in combination_strings:
        # except for the identity
        if "Z" in zi_string_combination:
            op_name = "".join(zi_string_combination)
            tensor_op = str_to_symbolic(op_name)
            # append in output_dict
            output_dict[op_name] = SymbolicHamiltonian(
                tensor_op, backend=backend
            ).dense.matrix
    return output_dict


def str_to_symbolic(name: str):
    """Convert string into symbolic hamiltonian.
    Example:
        .. testcode::

            from qibo.models.dbi.utils import str_to_symbolic
            op_name = "ZYXZI"
            # returns 5-qubit symbolic hamiltonian
            ZIXZI_op = str_to_symbolic(op_name)
    """
    tensor_op = 1
    for qubit, char in enumerate(name):
        tensor_op *= getattr(symbols, char)(qubit)
    return tensor_op


def select_best_dbr_generator(
    dbi_object: DoubleBracketIteration,
    d_list: list,
    step: Optional[float] = None,
    compare_canonical: bool = True,
    scheduling: DoubleBracketScheduling = None,
    **kwargs,
):
    """Selects the best double bracket rotation generator from a list and execute the rotation.

    Args:
        dbi_object (`DoubleBracketIteration`): the target DoubleBracketIteration object.
        d_list (list): list of diagonal operators (np.array) to run from.
        step (float): fixed iteration duration.
            Defaults to ``None``, optimize with `scheduling` method and `choose_step` function.
        compare_canonical (boolean): if `True`, the diagonalization effect with operators from `d_list` is compared with the canonical bracket.
        scheduling (`DoubleBracketScheduling`): scheduling method for finding the optimal step.

    Returns:
        The updated dbi_object, index of the optimal diagonal operator, respective step duration, and evolution direction.
    """
    if scheduling is None:
        scheduling = dbi_object.scheduling
    norms_off_diagonal_restriction = [dbi_object.off_diagonal_norm] * (len(d_list) + 1)
    optimal_steps = np.zeros(len(d_list) + 1)
    flip_list = np.ones(len(d_list) + 1)
    for i, d in enumerate(d_list):
        # prescribed step durations
        dbi_eval = deepcopy(dbi_object)
        flip_list[i] = cs_angle_sgn(dbi_eval, d)
        if flip_list[i] != 0:
            if step is None:
                step_best = dbi_eval.choose_step(
                    d=flip_list[i] * d, scheduling=scheduling, **kwargs
                )
            else:
                step_best = step
            dbi_eval(step=step_best, d=flip_list[i] * d)
            optimal_steps[i] = step_best
            norms_off_diagonal_restriction[i] = dbi_eval.off_diagonal_norm
    # canonical
    if compare_canonical is True:
        dbi_eval = deepcopy(dbi_object)
        dbi_eval.mode = DoubleBracketGeneratorType.canonical
        if step is None:
            step_best = dbi_eval.choose_step(scheduling=scheduling, **kwargs)
        else:
            step_best = step
        dbi_eval(step=step_best)
        optimal_steps[-1] = step_best
        norms_off_diagonal_restriction[-1] = dbi_eval.off_diagonal_norm
    # find best d
    idx_max_loss = np.argmin(norms_off_diagonal_restriction)
    flip = flip_list[idx_max_loss]
    step_optimal = optimal_steps[idx_max_loss]
    dbi_eval = deepcopy(dbi_object)
    if idx_max_loss == len(d_list) and compare_canonical is True:
        # canonical
        dbi_eval(step=step_optimal, mode=DoubleBracketGeneratorType.canonical)

    else:
        d_optimal = flip * d_list[idx_max_loss]
        dbi_eval(step=step_optimal, d=d_optimal)
    return dbi_eval, idx_max_loss, step_optimal, flip


def cs_angle_sgn(dbi_object, d):
    """Calculates the sign of Cauchy-Schwarz Angle :math:`\\langle W(Z), W({\\rm canonical}) \\rangle_{\\rm HS}`."""
    norm = np.trace(
        np.dot(
            np.conjugate(
                dbi_object.commutator(dbi_object.diagonal_h_matrix, dbi_object.h.matrix)
            ).T,
            dbi_object.commutator(d, dbi_object.h.matrix),
        )
    )
    return np.sign(norm)


def dGamma_di_Pauli(
    dbi_object: DoubleBracketIteration, n: int, Z_i: np.array, d: np.array
):
    """Computes the derivatives $\frac{\\partial \\Gamma_n}{\\partial \alpha_i}$ where the diagonal operator $D=\\sum \alpha_i Z_i$.

    Args:
        dbi_object (DoubleBracketIteration): the target dbi object
        n (int): the number of nested commutators in `Gamma`
        i (int/tupple): the index of onsite-Z coefficient
        d (np.array): the diagonal operator

    Returns:
        (list): [dGamma_0_di, dGamma_1_di, ..., dGamma_n_di]
    """
    nqubits = int(np.log2(dbi_object.h.matrix.shape[0]))
    dGamma_di = [np.zeros((2**nqubits, 2**nqubits))] * (n + 1)
    Gamma_list = dbi_object.generate_Gamma_list(n=n + 2, d=d)
    W = dbi_object.commutator(d, dbi_object.h.matrix)
    dW_di = dbi_object.commutator(Z_i, dbi_object.h.matrix)
    for k in range(n + 1):
        if k == 0:
            continue
        elif k == 1:
            dGamma_di[k] = dW_di
        else:
            dGamma_di[k] = dbi_object.commutator(
                dW_di, Gamma_list[k - 1]
            ) + dbi_object.commutator(W, dGamma_di[k - 1])
    return dGamma_di


def ds_di_Pauli(
    dbi_object: DoubleBracketIteration,
    d: np.array,
    Z_i: np.array,
    taylor_coef: Optional[list] = None,
):
    r"""Return the derivatives of the first 3 polynomial coefficients with respect to onsite Pauli-Z coefficients\
        Args:
            dbi_object (DoubleBracketIteration): the target dbi object
            d (np.array): the diagonal operator
            i (int): the index of onsite-Z coefficient
            taylor_coef (list): coefficients of `s` in the taylor expansion of math:`\\frac{\\partial ||\sigma(e^{sW}He^{-sW})||^2}{\\partial s}`, from the highest order to the lowest.
            onsite_Z_ops (list): onsite Z operators of `dbi_object.h`
        Returns:
            floats da, db, dc, ds
    """
    # generate the list of derivatives w.r.t ith Z operator coefficient
    dGamma_di = dGamma_di_Pauli(dbi_object, n=4, Z_i=Z_i, d=d)
    Gamma_list = dbi_object.generate_Gamma_list(n=4, d=d)

    def derivative_product(k1, k2):
        r"""Calculate the derivative of a product $\sigma(\Gamma(n1,i))@\sigma(\Gamma(n2,i))"""
        return dbi_object.sigma(dGamma_di[k1]) @ dbi_object.sigma(
            Gamma_list[k2]
        ) + dbi_object.sigma(dbi_object.sigma(Gamma_list[k1])) @ dbi_object.sigma(
            dGamma_di[k2]
        )

    # calculate the derivatives of s polynomial coefficients
    da = np.trace(3 * derivative_product(1, 2) + 3 * derivative_product(3, 0))
    db = np.trace(2 * derivative_product(1, 1) + 2 * derivative_product(0, 2))
    dc = np.trace(2 * derivative_product(1, 0))

    ds = 0
    if taylor_coef != None:
        a, b, c = taylor_coef[len(taylor_coef) - 3 :]
        delta = b**2 - 4 * a * c
        ddelta = 2 * (b * db - 2 * (a * dc + da * c))

        ds = (-db + 0.5 * ddelta / np.sqrt(delta)) * a - (-b + np.sqrt(delta)) * da
        ds /= 2 * a**2

    return da, db, dc, ds


def gradient_Pauli(
    dbi_object: DoubleBracketIteration,
    d: np.array,
    pauli_operator_dict: dict,
    use_ds=False,
    n=3,
    **kwargs,
):
    r"""Calculate the gradient of loss function with respect to onsite Pauli-Z coefficients
    Args:
        dbi_object (DoubleBracketIteration): the target dbi object
        d (np.array): the diagonal operator
        n_taylor (int): the highest order of the taylore expansion of  w.r.t `s`
        onsite_Z_ops (list): list of Pauli-Z operators
        taylor_coef (list): coefficients of `s` in the taylor expansion of math:`\\frac{\\partial ||\sigma(e^{sW}He^{-sW})||^2}{\\partial s}`
        use_ds (boolean): if False, ds is set to 0
    """
    # n is the highest order for calculating s

    # pauli_index is the list of positions \mu
    pauli_operators = list(pauli_operator_dict.values())
    num_paul = len(pauli_operators)
    grad = np.zeros(num_paul)
    coef = off_diagonal_norm_polynomial_expansion_coef(dbi_object, d, n=n)
    s = dbi_object.choose_step(
        d=d,
        **kwargs,
    )

    a, b, c = coef[len(coef) - 3 :]

    for i, operator in enumerate(pauli_operators):
        da, db, dc, ds = ds_di_Pauli(
            dbi_object, d=d, Z_i=operator, taylor_coef=[a, b, c]
        )
        if use_ds is True:
            ds = 0
        grad[i] = (
            s**3 / 3 * da
            + s**2 / 2 * db
            + 2 * s * dc
            + s**2 * ds * a
            + s * ds * b
            + 2 * ds * c
        )
    grad = np.array(grad)
    grad = grad / np.linalg.norm(grad)
    return grad, s


def decompose_into_Pauli_basis(h_matrix: np.array, pauli_operators: list):
    """finds the decomposition of hamiltonian `h_matrix` into Pauli-Z operators"""
    nqubits = int(np.log2(h_matrix.shape[0]))

    decomposition = []
    for Z_i in pauli_operators:
        expect = np.trace(h_matrix @ Z_i) / 2**nqubits
        decomposition.append(expect)
    return decomposition


def generate_pauli_index(nqubits, order):
    if order == 1:
        return list(range(nqubits))
    elif order > 1:
        indices = list(range(nqubits))
        return indices + [
            comb for i in range(2, order + 1) for comb in combinations(indices, i)
        ]
    else:
        raise ValueError("Order must be a positive integer")


def generate_pauli_operator_dict(nqubits: int, parameterization_order: int):
    pauli_index = generate_pauli_index(nqubits, order=parameterization_order)
    pauli_operators = [
        generate_Pauli_operators(nqubits, symbols.Z, index) for index in pauli_index
    ]
    return {index: operator for index, operator in zip(pauli_index, pauli_operators)}


def gradient_descent_pauli(
    dbi_object: DoubleBracketIteration,
    d_coef: list,
    d: Optional[np.array] = None,
    pauli_operator_dict: dict = None,
    parameterization_order: int = 1,
    n: int = 3,
    onsite_Z_ops: Optional[list] = None,
    lr_min: float = 1e-5,
    lr_max: float = 1,
    max_evals: int = 100,
    space: callable = None,
    optimizer: callable = None,
    verbose: bool = False,
    use_ds: bool = True,
):
    """calculate the elements of one gradient descent step on `dbi_object`.

    Args:
        dbi_object (DoubleBracketIteration): the target dbi object
        d_coef (list): the initial decomposition of `d` into Pauli-Z operators
        d (np.array, optional): the initial diagonal operator. Defaults to None.
        n_taylor (int, optional): the highest order to expand the loss function derivative. Defaults to 2.
        onsite_Z_ops (list, optional): list of onsite-Z operators. Defaults to None.
        lr_min (float, optional): the minimal gradient step. Defaults to 1e-5.
        lr_max (float, optional): the maximal gradient step. Defaults to 1.
        max_evals (int, optional): the max number of evaluations for `hyperopt` to find the optimal gradient step `lr`. Defaults to 100.
        space (callable, optional): the search space for `hyperopt`. Defaults to None.
        optimizer (callable, optional): optimizer for `hyperopt`. Defaults to None.
        verbose (bool, optional): option to print out the 'hyperopt' progress. Defaults to False.
        use_ds (bool, optional): if False, ds is set to 0. Defaults to True.

    Returns:
        the optimal step found, coeffcients of `d` in Pauli-Z basis, matrix of `d`

    """
    nqubits = int(np.log2(dbi_object.h.matrix.shape[0]))
    if pauli_operator_dict is None:
        pauli_operator_dict = generate_pauli_operator_dict(
            nqubits, parameterization_order
        )

    grad, s = gradient_Pauli(
        dbi_object, d, n=n, pauli_operator_dict=pauli_operator_dict, use_ds=use_ds
    )
    # optimize gradient descent step with hyperopt
    if space is None:
        space = hyperopt.hp.loguniform("lr", np.log(lr_min), np.log(lr_max))
    if optimizer is None:
        optimizer = hyperopt.tpe

    def func_loss_to_lr(lr):
        d_coef_eval = [d_coef[j] - grad[j] * lr for j in range(nqubits)]
        d_eval = sum(
            [
                d_coef_eval[i] * list(pauli_operator_dict.values())[i]
                for i in range(nqubits)
            ]
        )
        return dbi_object.loss(step=s, d=d_eval)

    best = hyperopt.fmin(
        fn=func_loss_to_lr,
        space=space,
        algo=optimizer.suggest,
        max_evals=max_evals,
        verbose=verbose,
    )
    lr = best["lr"]

    d_coef = [d_coef[j] - grad[j] * lr for j in range(nqubits)]
    d = sum([d_coef[i] * list(pauli_operator_dict.values())[i] for i in range(nqubits)])
    return s, d_coef, d


def diagonal_min_max(matrix: np.array):
    L = int(np.log2(matrix.shape[0]))
    D = np.linspace(np.min(np.diag(matrix)), np.max(np.diag(matrix)), 2**L)
    D = np.diag(D)
    return D


def off_diagonal_norm_polynomial_expansion_coef(dbi_object, d, n):
    if d is None:
        d = dbi_object.diagonal_h_matrix
    # generate Gamma's where $\Gamma_{k+1}=[W, \Gamma_{k}], $\Gamma_0=H
    W = dbi_object.commutator(d, dbi_object.sigma(dbi_object.h.matrix))
    Gamma_list = dbi_object.generate_Gamma_list(n + 2, d)
    sigma_Gamma_list = list(map(dbi_object.sigma, Gamma_list))
    exp_list = np.array([1 / math.factorial(k) for k in range(n + 1)])
    # coefficients for rotation with [W,H] and H
    c1 = exp_list.reshape((-1, 1, 1)) * sigma_Gamma_list[1:]
    c2 = exp_list.reshape((-1, 1, 1)) * sigma_Gamma_list[:-1]
    # product coefficient
    trace_coefficients = [0] * (2 * n + 1)
    for k in range(n + 1):
        for j in range(n + 1):
            power = k + j
            product_matrix = c1[k] @ c2[j]
            trace_coefficients[power] += 2 * np.trace(product_matrix)
    # coefficients from high to low (n:0)
    coef = list(reversed(trace_coefficients[: n + 1]))
    return coef


def generate_Pauli_operators(nqubits, symbols_pauli, positions):
    # generate matrix of an nqubit-pauli operator with `symbols_pauli` at `positions`
    if isinstance(positions, int):
        return SymbolicHamiltonian(
            symbols_pauli(positions), nqubits=nqubits
        ).dense.matrix
    else:
        terms = [symbols_pauli(pos) for pos in positions]
        return SymbolicHamiltonian(math.prod(terms), nqubits=nqubits).dense.matrix
