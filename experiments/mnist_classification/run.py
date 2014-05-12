#!/usr/bin/env python
import time
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from utils import rearrange_for_plot
import numpy as np

from eca import ECA
from utils import MnistData

conf = {
    'use_minibatches': False,
    'use_svm': False
}

if conf['use_svm']:
    from sklearn import svm
else:
    from sklearn.linear_model import LogisticRegression


class TestCaseBase(object):
    def __init__(self):
        self.batch_size = 10000  # Actually training input size (k)
        self.testset_size = 1000
        self.data = MnistData(batch_size=self.batch_size, testset_size=self.testset_size)
        self.trn_iters = 400
        self.mdl = None
        self.onehot = False
        self.y_in_u = False
        self.tau_start = None
        self.tau_end = None
        self.tau_alpha = None

        self.configure()
        assert self.mdl is not None

        (u, y) = self.data.get('trn', i=0, as_one_hot=self.onehot)

        # This is global slowness that helps the model to get past the
        # unstable beginning..
        tau = self.tau_start
        if self.onehot:
            y *= 9.2
            print 'energy u:', np.average(np.sum(np.square(u), axis=0)),
            print 'energy y:', np.average(np.sum(np.square(y), axis=0))

        if self.y_in_u:
            u = np.vstack([u, y])

        print 'Training...'
        try:
            t = time.time()
            for i in range(self.trn_iters):
                # This does not work. Visualization of FI * X becomes blurred (an
                # average of samples?)
                if conf['use_minibatches']:
                    assert False, "Not supported"
                    u, y = self.data.get('trn', i=0)

                self.mdl.update(u, y, tau)
                tau = tau * self.tau_alpha + (1 - self.tau_alpha) * self.tau_end

                i_str = "I %4d:" % (i + 1)
                # Progress prings
                if ((i + 1) % 20 == 0):
                    t_str = 't: %.2f s' % (time.time() - t)
                    t = time.time()
                    tau_str = "Tau: %4.1f" % tau

                    tostr = lambda t: "{" + ", ".join(["%s: %6.2f" % (n, v) for (n, v) in t]) + "}"

                    var_str = " logvar:" + tostr(self.mdl.variance())
                    a_str   = " avg:   " + tostr(self.mdl.avg_levels())
                    phi_str = " |phi|: " + tostr(self.mdl.phi_norms())

                    print i_str, tau_str, t_str, var_str, phi_str
                    #print var_str, a_str, phi_str
                    #print var_str, phi_str

                # Accuracy prints
                if ((i + 1) % 200 == 0):
                    t2 = time.time()
                    (trn_acc, val_acc) = self.calculate_accuracy(u, y)
                    t_str = 't: %.2f s,' % (time.time() - t2)
                    acc_str = "Accuracy trn %6.2f %%, val %6.2f %%" % (100. * trn_acc,
                                                              100. * val_acc)

                    print i_str, acc_str, t_str

            print 'Testing accuracy...'
            (ut, yt) = self.data.get('tst', as_one_hot=self.onehot)
            # TODO: test error broken at the moment, trust validation as it is not used for
            # hyper-parameter tuning at the moment
            #tst_acc = self.calculate_accuracy(ut, yt)
            tst_acc = np.nan
            print "Accuracy: Training %6.2f %%, validation %6.2f %%, test %6.2f %%" % (
                100. * trn_acc,
                100. * val_acc,
                100. * tst_acc)
        except KeyboardInterrupt:
            pass
        # Viz
        # Plot some energies ("lambdas")
        #e = np.array(self.mdl.energy())
        #f, ax = plt.subplots(len(e))
        #for (i, row) in enumerate(e):
            #ax[i].plot(np.sort(row)[::-1])
            #print "E(X%d)" % i, ", ".join(["%4.2f" % v for v in np.sort(row)[-1:-3:-1]]), '...', \
                #", ".join(["%4.2f" % v for v in np.sort(row)[0:3]])

        #plt.show()
        self.visualize()

    def configure(self):
        raise NotImplemented()

    def accuracy(self, y_est, y_true):
        y_true = y_true if len(y_true.shape) == 1 else np.argmax(y_true, axis=0)
        y_est = y_est if len(y_est.shape) == 1 else np.argmax(y_est, axis=0)
        return float(np.bincount(y_est == y_true, minlength=2)[1]) / len(y_est)

    def calculate_accuracy(self, u, y):
        if len(y.shape) == 2:
            y = np.argmax(y, axis=0)
        if conf['use_svm']:
            classifier = svm.LinearSVC()
            print 'Testing accuracy with SVM...'
        else:
            classifier = LogisticRegression()
            print 'Testing accuracy with logistic regression...'

        # y has the corresponding labels to the data used in teaching
        # so we can build the model now
        #self.mdl.fit_classifier(classifier, y)
        #def estimate(data):
            #state = self.mdl.converged_X(data)
            #y_est = classifier.predict(state.T)
            #return y_est

        def estimate(data):
            y = self.mdl.converged_U(data)
            print y.shape, y[-10:, :].shape
            return y[-10:, :]

        y_est = self.mdl.uest()[-10:, :]
        #y_est = estimate(u[:, :self.testset_size])
        trn_acc = self.accuracy(y_est[:, :self.testset_size], y.T[:self.testset_size].T)

        (uv, yv) = self.data.get('val', limit=self.batch_size, as_one_hot=self.onehot)
        if self.y_in_u:
            uv = np.vstack((uv, np.zeros((10, uv.shape[1]))))
        val_acc = self.accuracy(estimate(uv), yv)
        return (trn_acc, val_acc)

rect = lambda x: np.where(x < 0., 0., x)

class UnsuperLayer(TestCaseBase):
    def configure(self):
        layers = [20]  # Try e.g. [30, 20] for multiple layers and increase tau start
        self.tau_start = 30
        self.tau_end = 5
        self.tau_alpha = 0.99
        self.onehot = True
        self.y_in_u = True
        self.mdl = ECA(layers,
                       self.data.size('trn', 0)[0][0] + 10,
                       0,  # n of output
                       np.abs)  # np.tanh, rect, None, etc..

    def visualize(self):
        plt.imshow(rearrange_for_plot(self.mdl.first_phi()[:-10, :]), cmap=cm.Greys_r)
        plt.show()


def main():
    print 'Initializing...'
    UnsuperLayer()

if __name__ == '__main__':
    main()