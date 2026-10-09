import hashlib
import json

from pydantic import BaseModel, Field, model_validator

ACCOUNT_ID = r"^[A-Za-z0-9_.-]{1,64}$"
CURRENCY = r"^[A-Z]{3}$"


class AccountIn(BaseModel):
    id: str = Field(pattern=ACCOUNT_ID)
    currency: str = Field(pattern=CURRENCY)
    allow_negative: bool = False


class TransactionIn(BaseModel):
    from_account: str = Field(pattern=ACCOUNT_ID)
    to_account: str = Field(pattern=ACCOUNT_ID)
    amount: int = Field(gt=0, le=10**12, description="Minor units (e.g. cents)")
    currency: str = Field(pattern=CURRENCY)
    reference: str | None = Field(default=None, max_length=140)

    @model_validator(mode="after")
    def _distinct_accounts(self) -> "TransactionIn":
        if self.from_account == self.to_account:
            raise ValueError("from_account and to_account must differ")
        return self

    def fingerprint(self) -> str:
        """Stable hash used to detect an idempotency key reused with a different body."""
        canonical = json.dumps(self.model_dump(), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()
