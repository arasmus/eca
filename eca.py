from time import time

import numpy as np
import theano
import theano.tensor as T
DEBUG_INFO = False

from utils import rect
from theano.ifelse import ifelse
PRINT_CONVERGENCE = False
FLOATX = theano.config.floatX


def lerp(old, new, min_tau=0.0, en=None):
    """
    Return new interpolated value and a relative difference
    """
    diff = T.mean(T.sqr(new) - T.sqr(old), axis=1, keepdims=True)
    rel_diff = diff / (T.mean(T.sqr(old), axis=1, keepdims=True) + 1e-5)
    t = rel_diff * 20.
    t = T.where(t < 5, 5, t)
    t = T.where(t > 100, 100, t)
    t = t + min_tau
    if en is not None:
        lmbd = T.diagonal(en).dimshuffle(0, 'x') * (1. / t)
    else:
        lmbd = 1. / t
    return ((1 - lmbd) * old + lmbd * new,
            t, rel_diff)


class Signal(object):
    """ Object that represents any kind of state U, X, X_y, z, ...
    """
    def __init__(self, n, k, name, next):
        rng = np.random.RandomState(0)
        self.var = theano.shared(np.float32(rng.uniform(size=(n, k))), name=name)
        self.n = n
        self.k = k
        self.name = name
        self.modulation = None
        self.next = next

    def set_modulation(self, mod):
        assert self.modulation is None
        self.modulation = mod

    def variance(self):
        return np.average(np.log(np.var(self.var.get_value(), axis=1)))

    def energy(self):
        return np.average(np.square(self.var.get_value()), axis=1)


class LayerBase(object):
    def __init__(self, name, n, prev):
        self.n = n
        self.m = n if prev is None else prev.n
        self.name = name
        self.signal_key = name
        # Nonlinearity applied to the estimate coming from next layer
        self.nonlin_est = lambda x: x
        self.nonlin = None
        self.merge_op = None

        self.prev = prev
        self.next = None
        if self.prev:
            assert prev.next is None
            prev.next = self

    def signal(self, signals):
        key = self.signal_key
        if key not in signals.signal:
            next_sig = self.next.signal(signals) if self.next else None
            s = Signal(self.n, signals.k, self.name, next_sig)
            signals.signal[key] = s
        return signals.signal[key]

    def compile_prop_f(self, signals, has_input, min_tau=0.0):
        tau_in = T.scalar('min_tau', dtype=FLOATX)
        inputs = [tau_in]
        x = self.signal(signals)

        # Get estimate of the state from layer above
        estimate = self.estimate(signals)

        # Feedforward originates from previous layer's state or given input
        if not has_input:
            feedforward = self.feedforward(signals)
            has_nans = T.as_tensor_variable(0)
            nans = 0.0
        else:
            input_t = T.matrix('input', dtype=FLOATX)
            inputs += [input_t]
            nans = T.isnan(input_t)
            has_nans = T.any(nans)
            feedforward = T.where(nans, 0.0, input_t)

        self.info('Compiling propagation: %6s %4s %6s' %
                  (self.prev.name + ' ->' if self.prev else 'u/y ->',
                   self.name,
                   '<- ' + self.next.name if self.next else ""))

        # Apply nonlinearity to feedforward path only
        if self.nonlin:
            feedforward = self.nonlin(feedforward)

        if self.merge_op:
            new_value = self.merge_op(feedforward, estimate)
        else:
            new_value = feedforward - estimate

        # If predicting missing values, force them to zero in residual so
        # that they don't influence learning
        new_value = ifelse(has_nans, T.where(nans, 0.0, new_value), new_value)

        (new_X, t, d) = lerp(x.var, new_value, tau_in)
        d = T.max(d)

        return theano.function(inputs=inputs,
                               outputs=d,
                               updates=[(x.var, new_X)])

    def estimate(self, signals):
        """ Ask the next for feedback and apply nonlinearity """
        if not self.next:
            return 0.0
        return self.nonlin_est(self.next.feedback(signals))

    def feedback(self, signals):
        x = self.signal(signals)
        return T.dot(self.phi, x.var)

    def feedforward(self, signals):
        x = self.prev.signal(signals)
        return T.dot(self.phi.T, x.var)

    def info(self, str):
        if DEBUG_INFO:
            print '%5s:' % self.name, str


class Layer(LayerBase):
    def __init__(self, name, n, prev, nonlin, min_tau=0.0, stiffx=1.0):
        super(Layer, self).__init__(name, n, prev)
        n = self.n
        m = self.m
        rng = np.random.RandomState(0)
        self.nonlin = nonlin
        self.stiffx = stiffx
        self.min_tau = min_tau

        rand_init = np.float32(rng.uniform(size=(n, m)) - 0.5)
        self.E_XU = theano.shared(rand_init, name='E_XU')
        self.E_XX = theano.shared(np.identity(n, dtype=FLOATX), name='E_XX')
        self.Q = theano.shared(np.identity(n, dtype=FLOATX), name='Q')
        self.phi = theano.shared(rand_init.T, name='phi')

    def compile_adapt_f(self, signals):
        x = self.signal(signals)
        x_prev = self.prev.signal(signals)
        assert x_prev.k == x.k, "Sample size mismatch"
        assert x_prev.n == self.m, "Input dim mismatch"
        assert x.n == self.n, "Output dim mismatch"
        k = np.float32(x.k)
        # Modulate x
        if x.modulation is not None:
            x_ = x.var * T.as_tensor_variable(x.modulation)
        else:
            x_ = x.var
        (E_XU_new, t, d1) = lerp(self.E_XU,
                                 T.dot(x_, x_prev.var.T) / k,
                                 self.min_tau)
        (E_XX_new, t, d2) = lerp(self.E_XX,
                                 T.dot(x_, x_.T) / k,
                                 self.min_tau)
        E_XU_update = (self.E_XU, E_XU_new)
        E_XX_update = (self.E_XX, E_XX_new)
        b = 1.
        d = T.diagonal(E_XX_new)
        stiff = T.scalar('stiffnes', dtype=FLOATX)
        Q_new = theano.sandbox.linalg.ops.diag(b / T.where(d < stiff * self.stiffx,
                                                           stiff * self.stiffx, d))
        Q_update = (self.Q, Q_new)

        # TODO: optional spatial neighborhood coupling
        phi_update = (self.phi, T.dot(Q_new, E_XU_new).T)

        self.info('Compile layer update between: ' + self.name + ' and ' + self.prev.name)
        d = T.maximum(T.max(d1), T.max(d2))
        return theano.function(
            inputs=[stiff],
            outputs=d,
            updates=[E_XU_update, E_XX_update, Q_update, phi_update])

    def __str__(self):
        return "Layer %3s (%d) %.2f, %.2f, %s" % (self.name, self.n,
                                                  self.stiffx,
                                                  self.min_tau,
                                                  self.nonlin)


class Input(LayerBase):
    def __init__(self, name, n):
        super(Input, self).__init__(name, n, None)

    def compile_adapt_f(self, signals):
        return lambda stiff: 0.0

    def __str__(self):
        return "Input %3s (%d)" % (self.name, self.n)


class RegressionLayer(LayerBase):
    def __init__(self, name, n, (prev1, prev2), nonlin,
                 min_tau=0.0, stiffx=1.0, merge_op=None):
        super(RegressionLayer, self).__init__(name, n, None)

        self.u_side = Layer(name + 'u', n, prev1, lambda x: x, 0.0)
        self.y_side = Layer(name + 'y', n, prev2, lambda x: x, 0.0)
        self.u_side.signal_key = name
        self.y_side.signal_key = name
        # TODO: figure out how to expose bot u_side and y_side phi
        self.phi = self.u_side.phi

        # To make u and y update the same shared state, it must happen
        # simultaneously, so route u to ask y for its feedback as an estimate.
        self.u_side.estimate = lambda i: self.y_side.feedforward(i)
        #self.u_side.merge_op = lambda fromu, fromy: nonlin(fromy + fromu)
        #self.u_side.merge_op = lambda fromu, fromy: nonlin(fromu)
        self.u_side.merge_op = lambda fromu, fromy: nonlin(-fromy + fromu)
        #self.u_side.merge_op = lambda fromu, fromy: T.sqrt(fromu * fromy + 0.1)
        if merge_op:
            self.u_side.merge_op = merge_op

        # Disable state updates on the y side so that X is updated only once
        self.y_side.compile_prop_f = lambda s, is_input: lambda min_tau: 0.0

    def compile_adapt_f(self, signals):
        assert False, "should not be called"

    def __str__(self):
        return super(RegressionLayer, self).__str__() + str(self.merge_op)


class CCALayer(Layer):
    def __init__(self, name, (m, n)):
        self.E_ZZ = []
        super(CCALayer, self).__init__(name, (m, n), nonlin=None)
        # Phi of this layer is not in use, mark it as nan
        self.phi = np.zeros((1, 1))

    # TODO: Update
    def update_state(self, id, input, min_tau):
        z = self.X[id]
        E_ZZ = self.E_ZZ[id]
        assert input is None, "CCA state cannot use input"
        assert self.next is None, 'CCA cannot have next items'
        assert len(self.prev) == 2, 'CCA should have exactly 2 prevs'
        assert z.n == self.n, "Output dim mismatch"

        # Update state-specific E_ZZ and calculate q for z update
        (E_ZZ.value, di) = lerp(E_ZZ.value,
                                np.dot(z.value, z.value.T) / z.k)
        assert E_ZZ.value.shape == (1, 1), 'E_ZZ is not a scalar!?'
        b = 1.
        q = b / np.max([0.05, E_ZZ.value[0, 0]])

        # Update z
        [x1, x2] = [self.prev[j].X[id].value for j in (0, 1)]
        new_value = q * np.sum(x1 * x2, axis=0)

        (z.value, dz) = lerp(z.value, new_value)

        self.E_XU = None
        return np.max((np.abs(di), np.average(np.abs(dz))))

    # TODO: Update
    def feedback(self, id, prev=None):
        assert prev is not None, 'CCA needs prev'
        assert len(self.prev) == 2, 'CCA should have exactly 2 prevs'
        # XXX: Uncomment the following to avoid signal propagation
        # back from CCA layer
        #return np.zeros(())

        z = self.X[id]
        # Find the other branch connecting to this z
        x_other = list(set(self.prev) - set([prev]))[0]

        # This is roughly: x_u_delta = self.Q * x_y * z
        phi = self.Q * x_other.X[id].value
        return phi * z.value


class ECA(object):
    """
    Constructs chains of layers connected together. Each layer contains phi
    matrix and corresponding signal vector X.

    Typical loop u -- U -- X would represented by two of these layers; one for
    U and another one for X.

    U branch would look something like this, where --- indicates signal
    and /\\/ mapping:

      ------------  u

      //|\//\\/\|/  phi_0 = identity
     -------------- layer x0 = U = phi_1^T x-1 - phi_1 X1 = u - phi_1 X1

      //|\//\\/\|/  phi_1 (learned mapping)
     -------------- layer x1 = phi_1^T x0 - phi_2 x2

      //|\//\\/\|/  phi_2 (learned mapping)
     -------------- layer x2 = phi_2^T x1 - phi_3 x3
          .
          .
          .
    """

    def __init__(self):
        self.U = None
        self.Y = None
        self.structure()
        assert self.U is not None
        # TODO: Add more checks for layer structure, e.g. avoid loops
        for l in self.iter_layers():
            print l

    def structure(self):
        raise NotImplemented

    def iter_layers(self, skip_inputs=False):
        for l in [self.U, self.Y]:
            if skip_inputs and l:
                l = l.next
            while l:
                yield l
                l = l.next

    def new_signals(self, k):
        return Signals(k, self)

    def first_phi(self):
        # index is omitted for now, and the lowest layer is plotted
        return self.U.next.phi.get_value()

    def phi_norms(self):
        f = lambda l: (l.name, (np.linalg.norm(l.phi.get_value(), axis=0)))
        return map(f, self.iter_layers(skip_inputs=True))


class SimpleECA(ECA):
    """ Simplest possible one loop system """
    def __init__(self, n_input, n_layer):
        self.n_input = n_input
        self.n_layer = n_layer
        super(SimpleECA, self).__init__()

    def structure(self):
        self.U = Input('U', self.n_input)
        self.X = Layer('X', self.n_layer, self.U, rect, 1.0)


class Signals(object):
    def __init__(self, k, eca):
        self.mdl = eca
        self.k = k
        self.adaptf = {}
        self.propf = {}
        self.signal = {}
        self.name = None
        print 'Creating signals with k =', k

        for l in eca.iter_layers():
            is_input = l is eca.U or l is eca.Y
            self.propf[l.name] = l.compile_prop_f(self, is_input)
        self.U = self.signal[eca.U.name]
        self.Y = self.signal[eca.Y.name] if eca.Y else None

    def adapt_layers(self, stiffness):
        # Compile adaptation functions lazily
        if self.adaptf == {}:
            for l in self.mdl.iter_layers():
                self.adaptf[l.name] = l.compile_adapt_f(self)

        for l in self.mdl.iter_layers():
            self.adaptf[l.name](stiffness)

    def propagate(self, u, y, min_tau=0.0):
        assert u is None or self.k == u.shape[1], "Sample size mismatch"
        assert y is None or self.k == y.shape[1], "Sample size mismatch"
        d = 0.0
        for l in self.mdl.iter_layers():
            args = [min_tau]
            args += [u] if l is self.mdl.U else []
            args += [y] if l is self.mdl.Y else []
            d = max(d, self.propf[l.name](*args))
        return d

    def converge(self, u, y, min_tau=0.0, d_limit=1e-3):
        t = 20
        t_limit, i_limit = time() + t, 200
        d, i, = np.inf, 0
        while d > d_limit and time() < t_limit and i < i_limit:
            d = self.propagate(u, y, min_tau)
            i += 1
        if PRINT_CONVERGENCE:
            print 'Converged in', "%.1f" % (time() - t_limit + t), 's,',
            print i, 'iters, delta %.4f' % d,
            print 'Limits: i:', i_limit, 't:', t, 'd:', d_limit
        return self

    def x_est(self, no_eval=False):
        # TODO: Might not be reliable, fix.
        l = self.mdl.U
        while l.next and not isinstance(l.next, CCALayer):
            l = l.next
        # Should this be Xbar or the feedforward ?
        v = l.signal(self).var
        return v if no_eval else v.eval()

    def u_est(self, no_eval=False):
        v = self.mdl.U.estimate(self)
        return v if no_eval else v.eval()

    def y_est(self, no_eval=False):
        v = self.mdl.Y.estimate(self)
        return v if no_eval else v.eval()

    def u_err(self, u):
        return T.mean(T.sqr(self.u_est(no_eval=True) - u)).eval()

    def first_phi(self):
        # index is omitted for now, and the lowest layer is plotted
        return self.mdl.U.next.phi.get_value()

    def variance(self, states=None):
        f = lambda s: (s.name, s.variance())
        return map(f, self.signal.values())

    def energy(self):
        f = lambda s: (s.name, np.average(s.energy()))
        return map(f, self.signal.values())

    def avg_levels(self):
        f = lambda s: (s.name, np.linalg.norm(np.average(s.var.get_value(), axis=1)))
        return map(f, self.signal.values())

    def phi_norms(self):
        f = lambda l: (l.name, (np.linalg.norm(l.phi.get_value(), axis=0)))
        return map(f, self.iter_layers(skip_inputs=True))

