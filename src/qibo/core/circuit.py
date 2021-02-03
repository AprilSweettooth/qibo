# -*- coding: utf-8 -*-
# @authors: S. Efthymiou
import collections
from qibo import K
from qibo.abstractions import circuit
from qibo.config import raise_error
from qibo.core import measurements
from typing import List, Tuple


class Circuit(circuit.AbstractCircuit):
    """Backend implementation of :class:`qibo.abstractions.circuit.AbstractCircuit`.

    Performs simulation using state vectors.

    Example:
        ::

            from qibo import models, gates
            c = models.Circuit(3) # initialized circuit with 3 qubits
            c.add(gates.H(0)) # added Hadamard gate on qubit 0

    Args:
        nqubits (int): Total number of qubits in the circuit.
    """
    from qibo.core import fusion

    def __init__(self, nqubits):
        super(Circuit, self).__init__(nqubits)
        self.param_tensor_types = K.tensor_types
        self._compiled_execute = None
        self.check_initial_state_shape = True
        self.shapes = {
            'TENSOR': self.nqubits * (2,),
            'FLAT': (2 ** self.nqubits,)
        }
        self.shapes['TENSOR_FLAT'] = K.cast(self.shapes.get('FLAT'),
                                            dtype='DTYPEINT')

    def set_nqubits(self, gate):
        if gate.is_prepared and gate.nqubits != self.nqubits:
            raise_error(RuntimeError, "Cannot add gate {} that acts on {} "
                                      "qubits to circuit that contains {}"
                                      "qubits.".format(
                                            gate, gate.nqubits, self.nqubits))
        gate.nqubits = self.nqubits
        gate.prepare()

    def _add_layer(self, gate):
        gate.prepare()
        for unitary in gate.unitaries:
            self.set_nqubits(unitary)
            self.queue.append(unitary)
        if gate.additional_unitary is not None:
            self.set_nqubits(gate.additional_unitary)
            self.queue.append(gate.additional_unitary)

    def _fuse_copy(self):
        """Helper method for ``circuit.fuse``.

        For standard (non-distributed) circuits this creates a copy of the
        circuit with deep-copying the parametrized gates only.
        For distributed circuits a fully deep copy should be created.
        """
        import copy
        from qibo.abstractions.abstract_gates import ParametrizedGate
        new_circuit = self.__class__(**self.init_kwargs)
        for gate in self.queue:
            if isinstance(gate, ParametrizedGate):
                if gate.trainable:
                    new_gate = copy.copy(gate)
                    new_circuit.queue.append(new_gate)
                    new_circuit.parametrized_gates.append(new_gate)
                    new_circuit.trainable_gates.append(new_gate)
                else:
                    new_circuit.queue.append(gate)
                    new_circuit.parametrized_gates.append(gate)
            else:
                new_circuit.queue.append(gate)
        new_circuit.measurement_gate = copy.copy(self.measurement_gate)
        new_circuit.measurement_tuples = dict(self.measurement_tuples)
        return new_circuit

    def fuse(self):
        """Creates an equivalent ``Circuit`` with gates fused up to two-qubits.

        Returns:
            The equivalent ``Circuit`` object where the gates are fused.

        Example:
            ::

                from qibo import models, gates
                c = models.Circuit(2)
                c.add([gates.H(0), gates.H(1)])
                c.add(gates.CNOT(0, 1))
                c.add([gates.Y(0), gates.Y(1)])
                # create circuit with fused gates
                fused_c = c.fuse()
                # now ``fused_c`` contains only one ``gates.Unitary`` gate
                # that is equivalent to applying the five gates of the original
                # circuit.
        """
        from qibo.abstractions.circuit import _Queue
        new_circuit = self._fuse_copy()
        new_circuit.fusion_groups = self.fusion.FusionGroup.from_queue(
            new_circuit.queue)
        new_circuit.queue = _Queue(self.nqubits)
        for group in new_circuit.fusion_groups:
            for gate in group.gates:
                new_circuit.queue.append(gate)
        return new_circuit

    def _eager_execute(self, state):
        """Simulates the circuit gates in eager mode."""
        for gate in self.queue:
            state = gate(state)
        return state

    def _execute_for_compile(self, state):
        from qibo import gates
        callback_results = {gate.callback: [] for gate in self.queue
                            if hasattr(gate, "callback")}
        for gate in self.queue:
            if isinstance(gate, gates.CallbackGate): # pragma: no cover
                # compilation may be deprecated and is not sufficiently tested
                value = gate.callback(state)
                callback_results[gate.callback].append(value)
            else:
                state = gate(state)
        return state, callback_results

    def compile(self):
        """Compiles the circuit as a Tensorflow graph."""
        if self._compiled_execute is not None:
            raise_error(RuntimeError, "Circuit is already compiled.")
        if not self.queue:
            raise_error(RuntimeError, "Cannot compile circuit without gates.")
        if K.custom_gates:
            raise_error(RuntimeError, "Cannot compile circuit that uses custom "
                                      "operators.")
        self._compiled_execute = K.compile(self._execute_for_compile)

    def _execute(self, initial_state=None):
        """Performs all circuit gates on the state vector."""
        self._final_state = None
        state = self.get_initial_state(initial_state)
        if not K.custom_gates:
            state = K.reshape(state, self.shapes.get('TENSOR'))

        if self._compiled_execute is None:
            state = self._eager_execute(state)
        else:
            state, callback_results = self._compiled_execute(state)
            for callback, results in callback_results.items():
                callback.extend(results)

        if not K.custom_gates:
            state = K.reshape(state, self.shapes.get('TENSOR_FLAT'))

        self._final_state = state
        return state

    def _device_execute(self, initial_state=None):
        """Executes circuit on the specified device and checks for OOM errors."""
        device = K.default_device
        try:
            with K.device(device):
                state = self._execute(initial_state=initial_state)
        except K.oom_error:
            raise_error(RuntimeError, f"State does not fit in {device} memory."
                                       "Please switch the execution device to a "
                                       "different one using ``qibo.set_device``.")
        return state

    def _repeated_execute(self, nreps, initial_state=None):
        results = []
        for _ in range(nreps):
            state = self._device_execute(initial_state)
            if self.measurement_gate is not None:
                results.append(self.measurement_gate(state, nshots=1)[0])
                del(state)
            else:
                results.append(K.copy(state))
        results = K.stack(results, axis=0)

        if self.measurement_gate is None:
            return results

        mgate_result = measurements.GateResult(
                self.measurement_gate.qubits, decimal_samples=results)
        return measurements.CircuitResult(self.measurement_tuples, mgate_result)

    def execute(self, initial_state=None, nshots=None):
        """Propagates the state through the circuit applying the corresponding gates.

        In default usage the full final state vector is returned.
        If the circuit contains measurement gates and ``nshots`` is given, then
        the final state is sampled and the samples are returned. We refer to
        the :ref:`How to perform measurements? <measurement-examples>` example
        for more details on how to perform measurements in Qibo.

        If channels are found within the circuits gates then Qibo will perform
        the simulation by repeating the circuit execution ``nshots`` times.
        If the circuit contains measurements the corresponding noisy measurement
        result will be returned, otherwise the final state vectors will be
        collected to a ``(nshots, 2 ** nqubits)`` tensor and returned.
        The latter usage is memory intensive and not recommended.
        If the circuit is created with the ``density_matrix = True`` flag and
        contains channels, then density matrices will be used instead of
        repeated execution.
        Note that some channels (:class:`qibo.abstractions.gates.KrausChannel`) can
        only be simulated using density matrices and not repeated execution.
        For more details on noise simulation with and without density matrices
        we refer to :ref:`How to perform noisy simulation? <noisy-example>`

        Args:
            initial_state (array): Initial state vector as a numpy array of shape ``(2 ** nqubits,)``.
                A Tensorflow tensor with shape ``nqubits * (2,)`` is also allowed
                allowed as an initial state but must have the `dtype` of the circuit.
                If ``initial_state`` is ``None`` the |000...0> state will be used.
            nshots (int): Number of shots to sample if the circuit contains
                measurement gates.
                If ``nshots`` is ``None`` the measurement gates will be ignored.

        Returns:
            If ``nshots`` is given and the circuit contains measurements
                A :class:`qibo.core.measurements.CircuitResult` object that contains the measured bitstrings.
            If ``nshots`` is ``None`` or the circuit does not contain measurements.
                The final state vector as a tensor of shape ``(2 ** nqubits,)``.
        """
        if nshots is not None and self.repeated_execution:
            self._final_state = None
            return self._repeated_execute(nshots, initial_state)

        state = self._device_execute(initial_state)
        if self.measurement_gate is None or nshots is None:
            return state

        mgate_result = self.measurement_gate(state, nshots)
        return measurements.CircuitResult(self.measurement_tuples, mgate_result)

    @property
    def final_state(self):
        """Final state as a tensor of shape ``(2 ** nqubits,)``.

        The circuit has to be executed at least once before accessing this
        property, otherwise a ``ValueError`` is raised. If the circuit is
        executed more than once, only the last final state is returned.
        """
        if self._final_state is None:
            raise_error(RuntimeError, "Cannot access final state before the "
                                      "circuit is executed.")
        return self._final_state

    def _cast_initial_state(self, state):
        if isinstance(state, K.tensor_types):
            return K.cast(state)
        raise_error(TypeError, "Initial state type {} is not recognized."
                                "".format(type(state)))

    def get_initial_state(self, state=None):
        """"""
        if state is None:
            is_matrix = isinstance(self, DensityMatrixCircuit)
            return K.initial_state(self.nqubits, is_matrix)
        state = self._cast_initial_state(state)
        if self.check_initial_state_shape:
            shape = tuple(state.shape)
            if shape != self.shapes.get('FLAT'):
                raise_error(ValueError, "Invalid initial state shape {} for "
                                        "circuit with {} qubits."
                                        "".format(shape, self.nqubits))
        return state


class DensityMatrixCircuit(Circuit):
    """Backend implementation of :class:`qibo.abstractions.circuit.AbstractCircuit`.

    Performs simulation using density matrices. Can be initialized using the
    ``density_matrix=True`` flag and supports the use of channels.
    For more information on the use of density matrices we refer to the
    :ref:`Using density matrices? <densitymatrix-example>` example.

    Example:
        ::

            from qibo import models, gates
            c = models.Circuit(2, density_matrix=True)
            c.add(gates.H(0))
            c.add(gates.PauliNoiseChannel(1, px=0.2))

    Args:
        nqubits (int): Total number of qubits in the circuit.
    """

    def __init__(self, nqubits):
        super(DensityMatrixCircuit, self).__init__(nqubits)
        self.density_matrix = True
        self.shapes = {
            'TENSOR': 2 * self.nqubits * (2,),
            'VECTOR': (2 ** nqubits,),
            'FLAT': 2 * (2 ** self.nqubits,)
        }
        self.shapes['TENSOR_FLAT'] = K.cast(self.shapes.get('FLAT'),
                                            dtype='DTYPEINT')

    def _cast_initial_state(self, state):
        # Allow using state vectors as initial states but transform them
        # to the equivalent density matrix
        if tuple(state.shape) == self.shapes['VECTOR']:
            state = K.outer(state, K.conj(state))
        return Circuit._cast_initial_state(self, state)
