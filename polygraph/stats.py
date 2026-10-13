from math import comb


def _cdf(k, n, p):
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1))


def clopper_pearson(k: int, n: int, alpha: float = 0.05):
    if n == 0:
        return (None, None)
    def bisect(f, lo=0.0, hi=1.0):
        for _ in range(80):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) else (lo, mid)
        return (lo + hi) / 2
    lower = 0.0 if k == 0 else bisect(lambda p: (1 - _cdf(k - 1, n, p)) < alpha / 2)
    upper = 1.0 if k == n else bisect(lambda p: _cdf(k, n, p) > alpha / 2)
    return (lower, upper)


def fmt(k, n):
    if n == 0:
        return "n/a (0 cases)"
    lo, hi = clopper_pearson(k, n)
    return f"{k}/{n} = {k / n:.1%}  (95% CI {lo:.1%} to {hi:.1%})"
