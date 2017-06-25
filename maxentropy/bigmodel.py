import types
import math

import numpy as np
from scipy.misc import logsumexp

from .basemodel import BaseModel
from .maxentutils import innerprod, innerprodtranspose, vec_feature_function


class BigModel(BaseModel):
    """
    A maximum-entropy (exponential-form) model on a large sample space.

    The model expectations are not computed exactly (by summing or
    integrating over a sample space) but approximately (by Monte Carlo
    estimation).  Approximation is necessary when the sample space is too
    large to sum or integrate over in practice, like a continuous sample
    space in more than about 4 dimensions or a large discrete space like
    all possible sentences in a natural language.

    Approximating the expectations by sampling requires an instrumental
    distribution that should be close to the model for fast convergence.
    The tails should be fatter than the model.

    Parameters
    ----------
    auxiliary_sampler : callable
    
        Pass auxiliary_sampler as a function that will be used for importance
        sampling. When called with no arguments it should return a tuple
        (xs, log_q_xs) representing:

            xs: a sample x_1,...,x_n to use for importance sampling

            log_q_xs: an array of length n containing the (natural) log
                      probability density (pdf or pmf) of each point under the
                      auxiliary sampling distribution.


    Algorithms
    ----------
    The algorithm can be 'CG', 'BFGS', 'LBFGSB', 'Powell', or
    'Nelder-Mead'.

    The CG (conjugate gradients) method is the default; it is quite fast
    and requires only linear space in the number of parameters, (not
    quadratic, like Newton-based methods).

    The BFGS (Broyden-Fletcher-Goldfarb-Shanno) algorithm is a
    variable metric Newton method.  It is perhaps faster than the CG
    method but requires O(N^2) instead of O(N) memory, so it is
    infeasible for more than about 10^3 parameters.

    The Powell algorithm doesn't require gradients.  For small models
    it is slow but robust.  For big models (where func and grad are
    simulated) with large variance in the function estimates, this
    may be less robust than the gradient-based algorithms.
    """

    def __init__(self, auxiliary_sampler):
        super(BigModel, self).__init__()

        # We allow auxiliary_sampler to be a function or method or simply the
        # .__next__ method of a generator (which, curiously, isn't of MethodType).
        assert (isinstance(auxiliary_sampler, (types.FunctionType, types.MethodType))
                or (hasattr(auxiliary_sampler, '__name__') and auxiliary_sampler.__name__ == '__next__'))

        self.auxiliary_sampler = auxiliary_sampler

        # Number of sample matrices to generate and use to estimate E and logZ
        self.matrixtrials = 1

        # Store the lowest dual estimate observed so far in the fitting process
        self.bestdual = float('inf')

        # Most of the attributes below affect only the stochastic
        # approximation procedure.  They should perhaps be removed, and made
        # arguments of stochapprox() instead.

        # Use Kersten-Deylon accelerated convergence for stoch approx
        self.deylon = False

        # By default, use a stepsize decreasing as k^(-3/4)
        self.stepdecreaserate = 0.75

        # If true, check convergence using the exact model.  Only useful for
        # testing small problems (e.g. with different parameters) when
        # simulation is unnecessary.
        self.exacttest = False

        # By default use Ruppert-Polyak averaging for stochastic approximation
        self.ruppertaverage = True

        # Use the stoch approx scaling modification of Andradottir (1996)
        self.andradottir = False

        # Number of iterations to hold the stochastic approximation stepsize
        # a_k at a_0 for before decreasing it
        self.a_0_hold = 0

        # Whether or not to use the same sample for all iterations
        self.staticsample = True
        # If matrixtrials > 1 and staticsample = True, (which is useful for
        # estimating variance between the different feature estimates),
        # next(self.samplerFgen) will be called once for each trial
        # (0,...,matrixtrials) for each iteration.  This allows using a set
        # of feature matrices, each of which stays constant over all
        # iterations.

        # How many iterations of stochastic approximation between testing for
        # convergence
        self.testconvergefreq = 0

        # How many sample matrices to average over when testing for convergence
        # in stochastic approx
        self.testconvergematrices = 10

        # Test for convergence every 'testevery' iterations, using one or
        # more external samples. If 0, don't test.
        self.testevery = 0

    def setfeatures(self, f):
        """
        Set up a generator for feature matrices internally from a list of feature
        functions.

        Parameters
        ----------
        f : list of vectorized functions
            Each feature function must operate on a vector of samples xs =
            {x1,...,xn}, either real data or samples generated by the auxiliary
            sampler.
            
            If your feature functions are not vectorized, you can wrap them in
            calls to np.vectorize(f_i), but beware the performance overhead.
        """
        self.features = vec_feature_function(f, sparse=self.sparse)
        self.samplegen = feature_sampler(self.features, self.auxiliary_sampler)
        self.resample()

    def _check_features(self):
        """
        Validation of whether the feature matrix has been set properly
        """
        # Ensure the sample matrix has been set
        if not (hasattr(self, 'sampleF') and hasattr(self, 'samplelogprobs')):
            raise AttributeError("first specify a sample feature matrix"
                                 " using setfeatures()")

    def fit(self, f, K):
        """Fit the maxent model p whose feature expectations <f_i(X)> are given
        by the vector K_i.

        Parameters
        ----------
        f : either (a) list of (vectorized) functions or (b) 2d array

            (a) list of vectorized functions: [f_1, ..., f_m] 
                The functions f_i(x) take values x or vector values xs =
                [x_1,...,x_n] on the sample space and each returns a real
                value.

            (b) array: 2d array of shape (m, n)
                Matrix representing evaluations of f_i(x) on all random
                points generated by the sampler.
    
        K : array
            desired expectation values <f_i(X)> to set as constraints
            on the model p(X).

        Notes
        -----
        Model expectations are computed using Monte Carlo simulation.

        For 'BigModel' instances, the model expectations are not computed
        exactly (by summing or integrating over a sample space) but
        approximately (by Monte Carlo simulation).  Simulation is necessary
        when the sample space is too large to sum or integrate over in
        practice, like a continuous sample space in more than about 4
        dimensions or a large discrete space like all possible sentences in a
        natural language.

        Approximating the expectations by sampling requires an instrumental
        distribution that should be close to the model for fast convergence.
        The tails should be fatter than the model.  This instrumental
        distribution is specified by calling setfeatures().
        """
        super(BigModel, self).fit(f, K)

    def resample(self):
        """
        (Re)sample the matrix F of sample features, sample log probs, and
        (optionally) sample points too.
        """

        if self.verbose >= 3:
            print("(sampling)")

        # First delete the existing sample matrix to save memory
        # This matters, since these can be very large
        if hasattr(self, 'sampleF'):
            del self.sampleF
        if hasattr(self, 'samplelogprobs'):
            del self.samplelogprobs
        if hasattr(self, 'sample'):
            del self.sample

        # Now generate a new sample
        output = next(self.samplegen)

        # Assume the format is (F, lp, sample)
        (self.sampleF, self.samplelogprobs, self.sample) = output

        # Check whether the number m of features and the dimensionalities are correct
        m, n = self.sampleF.shape
        try:
            # The number of features is defined as the length of
            # self.params, so first check if it exists:
            self.params
        except AttributeError:
            self.params = np.zeros(m, float)
        else:
            if m != len(self.params):
                raise ValueError("the sample feature generator returned"
                                  " a feature matrix of incorrect dimensions."
                                  " The number of rows must equal the number of model parameters.")

        # Check the dimensionality of samplelogprobs is correct. It should be 1d, of length n
        if not (isinstance(self.samplelogprobs, np.ndarray) and self.samplelogprobs.shape == (n,)):
            raise ValueError('Your sampler appears to be spitting out logprobs of the wrong dimensionality.')

        if self.verbose >= 3:
            print("(done)")

        # Now clear the temporary variables that are no longer correct for this
        # sample
        self.clearcache()


    def lognormconst(self):
        """Estimate the normalization constant (partition function) using
        the current sample matrix F.
        """
        # First see whether logZ has been precomputed
        if hasattr(self, 'logZapprox'):
            return self.logZapprox

        # Compute log v = log [p_dot(s_j)/aux_dist(s_j)]   for
        # j=1,...,n=|sample| using a precomputed matrix of sample
        # features.
        logv = self._logv()

        # Good, we have our logv.  Now:
        n = len(logv)
        self.logZapprox = logsumexp(logv) - math.log(n)
        return self.logZapprox


    def expectations(self):
        """
        Estimate the feature expectations E_p[f(X)] under the current
        model p = p_theta using the given sample feature matrix.
        
        If self.staticsample is True, uses the current feature matrix
        self.sampleF.  If self.staticsample is False or self.matrixtrials
        is > 1, draw one or more sample feature matrices F afresh using
        the generator function samplegen().
        """
        # See if already computed
        if hasattr(self, 'mu'):
            return self.mu
        self.estimate()
        return self.mu

    def _logv(self):
        """This function helps with caching of interim computational
        results.  It is designed to be called internally, not by a user.

        Returns
        -------
        logv : 1d ndarray
               The array of unnormalized importance sampling weights
               corresponding to the sample x_j whose features are represented
               as the columns of self.sampleF.

               Defined as:

                   logv_j = p_dot(x_j) / q(x_j),

               where p_dot(x_j) = p_0(x_j) exp(theta . f(x_j)) is the
               unnormalized pdf value of the point x_j under the current model.
        """
        # First see whether logv has been precomputed
        if hasattr(self, 'logv'):
            return self.logv

        # Compute log v = log [p_dot(s_j)/aux_dist(s_j)]   for
        # j=1,...,n=|sample| using a precomputed matrix of sample
        # features.
        if self.external is None:
            paramsdotF = innerprodtranspose(self.sampleF, self.params)
            logv = paramsdotF - self.samplelogprobs
            # Are we minimizing KL divergence between the model and a prior
            # density p_0?
            if self.priorlogprobs is not None:
                logv += self.priorlogprobs
        else:
            e = self.external
            paramsdotF = innerprodtranspose(self.externalFs[e], self.params)
            logv = paramsdotF - self.externallogprobs[e]
            # Are we minimizing KL divergence between the model and a prior
            # density p_0?
            if self.externalpriorlogprobs is not None:
                logv += self.externalpriorlogprobs[e]

        # Good, we have our logv.  Now:
        self.logv = logv
        return logv

    def estimate(self):
        """
        Approximate both the feature expectation vector E_p f(X) and the log
        of the normalization term Z with importance sampling.

        This function also computes the sample variance of the component
        estimates of the feature expectations as: varE = var(E_1, ..., E_T)
        where T is self.matrixtrials and E_t is the estimate of E_p f(X)
        approximated using the 't'th auxiliary feature matrix.

        It doesn't return anything, but stores the member variables
        logZapprox, mu and varE.  (This is done because some optimization
        algorithms retrieve the dual fn and gradient fn in separate
        function calls, but we can compute them more efficiently
        together.)

        It uses a supplied generator sampleFgen whose __next__() method
        returns features of random observations s_j generated according
        to an auxiliary distribution aux_dist.  It uses these either in a
        matrix (with multiple runs) or with a sequential procedure, with
        more updating overhead but potentially stopping earlier (needing
        fewer samples).  In the matrix case, the features F={f_i(s_j)}
        and vector [log_aux_dist(s_j)] of log probabilities are generated
        by calling resample().

        We use [Rosenfeld01Wholesentence]'s estimate of E_p[f_i] as:
            {sum_j  p(s_j)/aux_dist(s_j) f_i(s_j) }
              / {sum_j p(s_j) / aux_dist(s_j)}.

        Note that this is consistent but biased.

        This equals:
            {sum_j  p_dot(s_j)/aux_dist(s_j) f_i(s_j) }
              / {sum_j p_dot(s_j) / aux_dist(s_j)}

        Compute the estimator E_p f_i(X) in log space as:
            num_i / denom,
        where
            num_i = exp(logsumexp(theta.f(s_j) - log aux_dist(s_j)
                        + log f_i(s_j)))
        and
            denom = [n * Zapprox]

        where Zapprox = exp(self.lognormconst()).

        We can compute the denominator n*Zapprox directly as:
            exp(logsumexp(log p_dot(s_j) - log aux_dist(s_j)))
          = exp(logsumexp(theta.f(s_j) - log aux_dist(s_j)))
        """

        if self.verbose >= 3:
            print("(estimating dual and gradient ...)")

        # Hereafter is the matrix code

        mus = []
        logZs = []

        for trial in range(self.matrixtrials):
            if self.verbose >= 2 and self.matrixtrials > 1:
                print("(trial " + str(trial) + " ...)")

            # Resample if necessary
            if (not self.staticsample) or self.matrixtrials > 1:
                self.resample()

            logv = self._logv()
            n = len(logv)
            logZ = self.lognormconst()
            logZs.append(logZ)

            # We don't need to handle negative values separately,
            # because we don't need to take the log of the feature
            # matrix sampleF. See Ed Schofield's PhD thesis, Section 4.4

            logu = logv - logZ
            if self.external is None:
                averages =  self.sampleF.dot(np.exp(logu))
            else:
                averages = self.externalFs[self.external].dot(np.exp(logu))
            averages /= n
            mus.append(averages)

        # Now we have T=trials vectors of the sample means.  If trials > 1,
        # estimate st dev of means and confidence intervals
        ttrials = len(mus)   # total number of trials performed
        if ttrials == 1:
            self.mu = mus[0]
            self.logZapprox = logZs[0]
            try:
                del self.varE       # make explicit that this has no meaning
            except AttributeError:
                pass
        else:
            # The log of the variance of logZ is:
            #     -log(n-1) + logsumexp(2*log|Z_k - meanZ|)

            self.logZapprox = logsumexp(logZs) - math.log(ttrials)
            stdevlogZ = np.array(logZs).std()
            mus = np.array(mus)
            self.varE = columnvariances(mus)
            self.mu = columnmeans(mus)


    # def setsampleFgen(self, sampler):
    #     """
    #     Initialize the Monte Carlo sampler to use the supplied
    #     generator of samples' features and log probabilities.  This is an
    #     alternative to defining a sampler in terms of a (fixed size)
    #     feature matrix sampleF and accompanying vector samplelogprobs of
    #     log probabilities.

    #     The output of next(sampler) can optionally be a 3-tuple (F, lp,
    #     sample) instead of a 2-tuple (F, lp).  In this case the value
    #     'sample' is then stored as a class variable self.sample.  This is
    #     useful for inspecting the output and understanding the model
    #     characteristics.

    #     (An alternative was to supply a list of samplers,
    #     sampler=[sampler0, sampler1, ..., sampler_{m-1}, samplerZ], one
    #     for each feature and one for estimating the normalization
    #     constant Z. But this code was unmaintained, and has now been
    #     removed (but it's in Ed's CVS repository :).)
    #     """

    def pdf(self, fx):
        """Returns the estimated density p_theta(x) at the point x with
        feature statistic fx = f(x).  This is defined as
            p_theta(x) = exp(theta.f(x)) / Z(theta),
        where Z is the estimated value self.normconst() of the partition
        function.
        """
        return np.exp(self.logpdf(fx))

    def pdf_function(self):
        """Returns the estimated density p_theta(x) as a function p(f)
        taking a vector f = f(x) of feature statistics at any point x.
        This is defined as:
            p_theta(x) = exp(theta.f(x)) / Z
        """
        log_Z_est = self.lognormconst()

        def p(fx):
            return np.exp(innerprodtranspose(fx, self.params) - log_Z_est)
        return p


    def logpdf(self, fx, log_prior_x=None):
        """Returns the log of the estimated density p(x) = p_theta(x) at
        the point x.  If log_prior_x is None, this is defined as:
            log p(x) = theta.f(x) - log Z
        where f(x) is given by the (m x 1) array fx.

        If, instead, fx is a 2-d (m x n) array, this function interprets
        each of its rows j=0,...,n-1 as a feature vector f(x_j), and
        returns an array containing the log pdf value of each point x_j
        under the current model.

        log Z is estimated using the auxiliary sampler provided.

        The optional argument log_prior_x is the log of the prior density
        p_0 at the point x (or at each point x_j if fx is 2-dimensional).
        The log pdf of the model is then defined as
            log p(x) = log p0(x) + theta.f(x) - log Z
        and p then represents the model of minimum KL divergence D(p||p0)
        instead of maximum entropy.
        """
        log_Z_est = self.lognormconst()
        if len(fx.shape) == 1:
            logpdf = np.dot(self.params, fx) - log_Z_est
        else:
            logpdf = innerprodtranspose(fx, self.params) - log_Z_est
        if log_prior_x is not None:
            logpdf += log_prior_x
        return logpdf


    def stochapprox(self, K):
        """Tries to fit the model to the feature expectations K using
        stochastic approximation, with the Robbins-Monro stochastic
        approximation algorithm: theta_{k+1} = theta_k + a_k g_k - a_k
        e_k where g_k is the gradient vector (= feature expectations E -
        K) evaluated at the point theta_k, a_k is the sequence a_k = a_0
        / k, where a_0 is some step size parameter defined as self.a_0 in
        the model, and e_k is an unknown error term representing the
        uncertainty of the estimate of g_k.  We assume e_k has nice
        enough properties for the algorithm to converge.
        """
        if self.verbose:
            print("Starting stochastic approximation...")

        # If we have resumed fitting, adopt the previous parameter k
        try:
            k = self.paramslogcounter
            #k = (self.paramslog-1)*self.paramslogfreq
        except:
            k = 0

        try:
            a_k = self.a_0
        except AttributeError:
            raise AttributeError("first define the initial step size a_0")

        avgparams = self.params
        if self.exacttest:
            # store exact error each testconvergefreq iterations
            self.SAerror = []
        while True:
            k += 1
            if k > self.a_0_hold:
                if not self.deylon:
                    n = k - self.a_0_hold
                elif k <= 2 + self.a_0_hold:   # why <= 2?
                    # Initialize n for the first non-held iteration
                    n = k - self.a_0_hold
                else:
                    # Use Kersten-Deylon accelerated SA, based on the rate of
                    # changes of sign of the gradient.  (If frequent swaps, the
                    # stepsize is too large.)
                    #n += (np.dot(y_k, y_kminus1) < 0)   # an indicator fn
                    if np.dot(y_k, y_kminus1) < 0:
                        n += 1
                    else:
                        # Store iterations of sign switches (for plotting
                        # purposes)
                        try:
                            self.nosignswitch.append(k)
                        except AttributeError:
                            self.nosignswitch = [k]
                        print("No sign switch at iteration " + str(k))
                    if self.verbose >= 2:
                        print("(using Deylon acceleration.  n is " + str(n) + " instead of " + str(k - self.a_0_hold) + "...)")
                if self.ruppertaverage:
                    if self.stepdecreaserate is None:
                        # Use log n / n as the default.  Note: this requires a
                        # different scaling of a_0 than a stepsize decreasing
                        # as, e.g., n^(-1/2).
                        a_k = 1.0 * self.a_0 * math.log(n) / n
                    else:
                        # I think that with Ruppert averaging, we need a
                        # stepsize decreasing as n^(-p), where p is in the open
                        # interval (0.5, 1) for almost sure convergence.
                        a_k = 1.0 * self.a_0 / (n ** self.stepdecreaserate)
                else:
                    # I think we need a stepsize decreasing as n^-1 for almost
                    # sure convergence
                    a_k = 1.0 * self.a_0 / (n ** self.stepdecreaserate)
            # otherwise leave step size unchanged
            if self.verbose:
                print("  step size is: " + str(a_k))

            self.matrixtrials = 1
            self.staticsample = False
            if self.andradottir:    # use Andradottir (1996)'s scaling?
                self.estimate()   # resample and reestimate
                y_k_1 = self.mu - K
                self.estimate()   # resample and reestimate
                y_k_2 = self.mu - K
                y_k = y_k_1 / max(1.0, norm(y_k_2)) + \
                      y_k_2 / max(1.0, norm(y_k_1))
            else:
                # Standard Robbins-Monro estimator
                if not self.staticsample:
                    self.estimate()   # resample and reestimate
                try:
                    y_kminus1 = y_k    # store this for the Deylon acceleration
                except NameError:
                    pass               # if we're on iteration k=1, ignore this
                y_k = self.mu - K
            norm_y_k = norm(y_k)
            if self.verbose:
                print("SA: after iteration " + str(k))
                print("  approx dual fn is: " + str(self.logZapprox \
                            - np.dot(self.params, K)))
                print("  norm(mu_est - k) = " + str(norm_y_k))

            # Update params (after the convergence tests too ... don't waste the
            # computation.)
            if self.ruppertaverage:
                # Use a simple average of all estimates so far, which
                # Ruppert and Polyak show can converge more rapidly
                newparams = self.params - a_k*y_k
                avgparams = (k-1.0)/k*avgparams + 1.0/k * newparams
                if self.verbose:
                    print("  new params[0:5] are: " + str(avgparams[0:5]))
                self.setparams(avgparams)
            else:
                # Use the standard Robbins-Monro estimator
                self.setparams(self.params - a_k*y_k)

            if k >= self.maxiter:
                print("Reached maximum # iterations during stochastic" \
                        " approximation without convergence.")
                break


    def settestsamples(self, F_list, logprob_list, testevery=1, priorlogprob_list=None):
        """Requests that the model be tested every 'testevery' iterations
        during fitting using the provided list F_list of feature
        matrices, each representing a sample {x_j} from an auxiliary
        distribution q, together with the corresponding log probabiltiy
        mass or density values log {q(x_j)} in logprob_list.  This is
        useful as an external check on the fitting process with sample
        path optimization, which could otherwise reflect the vagaries of
        the single sample being used for optimization, rather than the
        population as a whole.

        If self.testevery > 1, only perform the test every self.testevery
        calls.

        If priorlogprob_list is not None, it should be a list of arrays
        of log(p0(x_j)) values, j = 0,. ..., n - 1, specifying the prior
        distribution p0 for the sample points x_j for each of the test
        samples.
        """
        # Sanity check
        assert len(F_list) == len(logprob_list)

        self.testevery = testevery
        self.externalFs = F_list
        self.externallogprobs = logprob_list
        self.externalpriorlogprobs = priorlogprob_list

        # Store the dual and mean square error based on the internal and
        # external (test) samples.  (The internal sample is used
        # statically for sample path optimization; the test samples are
        # used as a control for the process.)  The hash keys are the
        # number of function or gradient evaluations that have been made
        # before now.

        # The mean entropy dual and mean square error estimates among the
        # t external (test) samples, where t = len(F_list) =
        # len(logprob_list).
        self.external_duals = {}
        self.external_gradnorms = {}


    def test(self):
        """Estimate the dual and gradient on the external samples,
        keeping track of the parameters that yield the minimum such dual.
        The vector of desired (target) feature expectations is stored as
        self.K.
        """
        if self.verbose:
            print("  max(params**2)    = " + str((self.params**2).max()))

        if self.verbose:
            print("Now testing model on external sample(s) ...")

        # Estimate the entropy dual and gradient for each sample.  These
        # are not regularized (smoothed).
        dualapprox = []
        gradnorms = []
        for e in range(len(self.externalFs)):
            self.external = e
            self.clearcache()
            if self.verbose >= 2:
                print("(testing with sample %d)" % e)
            dualapprox.append(self.dual(ignorepenalty=True, ignoretest=True))
            gradnorms.append(norm(self.grad(ignorepenalty=True)))

        # Reset to using the normal sample matrix sampleF
        self.external = None
        self.clearcache()

        meandual = np.average(dualapprox,axis=0)
        self.external_duals[self.iters] = dualapprox
        self.external_gradnorms[self.iters] = gradnorms

        if self.verbose:
            print("** Mean (unregularized) dual estimate from the %d" \
                  " external samples is %f" % \
                 (len(self.externalFs), meandual))
            print("** Mean mean square error of the (unregularized) feature" \
                    " expectation estimates from the external samples =" \
                    " mean(|| \hat{\mu_e} - k ||,axis=0) =", np.average(gradnorms,axis=0))
        # Track the parameter vector params with the lowest mean dual estimate
        # so far:
        if meandual < self.bestdual:
            self.bestdual = meandual
            self.bestparams = self.params
            if self.verbose:
                print("\n\t\t\tStored new minimum entropy dual: %f\n" % meandual)


def feature_sampler(vec_f, auxiliary_sampler):
    """
    A generator function for tuples (F, log_q_xs, xs)

    Parameters
    ----------
    vec_f : function
        Pass `vec_f` as a (vectorized) function that operates on a vector of
        samples xs = {x1,...,xn} and returns a feature matrix (m x n), where m
        is some number of feature components.

    auxiliary_sampler : function
        Pass `auxiliary_sampler` as a function that returns a tuple
        (xs, log_q_xs) representing a sample to use for sampling (e.g.
        importance sampling) on the sample space of the model.

    Yields
    ------
        tuples (F, log_q_xs, xs)

    """
    while True:
        xs, log_q_xs = auxiliary_sampler()
        F = vec_f(xs)  # compute feature matrix from points
        yield F, log_q_xs, xs
