from .adapters import InferenceUnavailable


class BudgetExhausted(InferenceUnavailable):
    """Raised instead of calling the model when a cap is reached."""


class BudgetedAdapter:
    def __init__(self, inner, charge):
        self.inner, self.charge = inner, charge
        self.model_id = getattr(inner, "model_id", "unknown")

    def parse(self, text):
        if not self.charge():
            raise BudgetExhausted("budget_exhausted")
        return self.inner.parse(text)
