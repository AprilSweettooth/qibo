import os
from qibo.backends import abstract, numpy
from qibo.config import LOG_LEVEL, raise_error


class Optimization:

    def __init__(self):
        import tensorflow as tf
        self.Variable = tf.Variable
        self.GradientTape = tf.GradientTape
        self.optimizers = tf.optimizers


class TensorflowBackend(numpy.NumpyBackend):

    def __init__(self):
        super().__init__()
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = str(LOG_LEVEL)
        import tensorflow as tf
        self.backend = tf
        self.name = "tensorflow"

        self.cpu_devices = tf.config.list_logical_devices("CPU")
        self.gpu_devices = tf.config.list_logical_devices("GPU")
        if self.gpu_devices: # pragma: no cover
            # CI does not use GPUs
            self.default_device = self.gpu_devices[0].name
        elif self.cpu_devices:
            self.default_device = self.cpu_devices[0].name
        else: # pragma: no cover
            # case not tested by GitHub workflows because it requires no device
            raise_error(RuntimeError, "Unable to find Tensorflow devices.")

        from qibo.backends import matrices
        self.matrices = matrices.TensorflowMatrices(self.dtypes('DTYPECPX'))

        self.tensor_types = (self.np.ndarray, tf.Tensor, tf.Variable)
        self.Tensor = tf.Tensor
        self.random = tf.random
        self.newaxis = tf.newaxis
        from tensorflow.python.framework import errors_impl # pylint: disable=E0611
        self.oom_error = errors_impl.ResourceExhaustedError
        self.optimization = Optimization()

        from qibo.tensorflow import custom_operators as op
        self.op = op

    def set_device(self, name):
        abstract.AbstractBackend.set_device(self, name)

    def cast(self, x, dtype='DTYPECPX'):
        if isinstance(dtype, str):
            dtype = self.dtypes(dtype)
        if isinstance(x, self.np.ndarray):
            dtypestr = dtype.__repr__().split(".")[1]
            x = x.astype(getattr(self.np, dtypestr))
        return self.backend.cast(x, dtype=dtype)

    def diag(self, x, dtype='DTYPECPX'):
        if isinstance(dtype, str):
            dtype = self.dtypes(dtype)
        return self.backend.cast(self.backend.linalg.diag(x), dtype=dtype)

    def concatenate(self, x, axis=None):
        return self.backend.concat(x, axis=axis)

    def copy(self, x):
        return x + self.backend.zeros_like(x)

    def range(self, start, finish, step, dtype=None):
        if isinstance(dtype, str):
            dtype = self.dtypes(dtype)
        return self.backend.range(start, finish, step, dtype=dtype)

    def real(self, x):
        return self.backend.math.real(x)

    def imag(self, x):
        return self.backend.math.imag(x)

    def conj(self, x):
        return self.backend.math.conj(x)

    def mod(self, x, y):
        return self.backend.math.mod(x, y)

    def right_shift(self, x, y):
        return self.backend.bitwise.right_shift(x, y)

    def pow(self, base, exponent):
        return self.backend.math.pow(base, exponent)

    def square(self, x):
        return self.backend.square(x)

    def sqrt(self, x):
        return self.backend.math.sqrt(x)

    def log(self, x):
        return self.backend.math.log(x)

    def trace(self, x):
        return self.backend.linalg.trace(x)

    def expm(self, x):
        return self.backend.linalg.expm(x)

    def sum(self, x, axis=None):
        return self.backend.reduce_sum(x, axis=axis)

    def outer(self, x, y):
        return self.tensordot(x, y, axes=0)

    def kron(self, x, y):
        raise_error(NotImplementedError)

    def inv(self, x):
        raise_error(NotImplementedError)

    def gather(self, x, indices=None, condition=None, axis=0):
        if indices is not None:
            return self.backend.gather(x, indices, axis=axis)

        if condition is None:
            raise_error(ValueError, "Gather call is missing indices and "
                                    "condition.")
        indices = self.backend.where(condition)
        return self.backend.gather(x, indices, axis=axis)[:, 0]

    def gather_nd(self, x, indices):
        return self.backend.gather_nd(x, indices)

    def initial_state(self, nqubits, is_matrix=False):
        from qibo.config import get_threads
        return self.op.initial_state(nqubits, self.dtypes('DTYPECPX'),
                                     is_matrix=is_matrix,
                                     omp_num_threads=get_threads())

    def random_uniform(self, shape, dtype='DTYPE'):
        return self.backend.random.uniform(shape, dtype=self.dtypes(dtype))

    def sample_measurements(self, probs, nshots):
        logits = self.log(probs)[self.newaxis]
        samples_dec = self.random.categorical(
            logits, nshots, dtype=self.dtypes('DTYPEINT'))
        return samples_dec[0]

    def compile(self, func):
        return self.backend.function(func)

    def device(self, device_name):
        return self.backend.device(device_name)

    def executing_eagerly(self):
        return self.backend.executing_eagerly()

    def set_seed(self, seed):
        self.backend.random.set_seed(seed)
