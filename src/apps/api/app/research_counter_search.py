"""Explicit research intent for counter-evidence searches."""
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CounterSearchIntent(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    target: str = Field(min_length=3, max_length=300, description='Specific claim or hypothesis being challenged; it need not be saved yet.')
    hypothesis: str = Field(min_length=3, max_length=500, description='A specific possible counter-explanation or observation sought.')

    @field_validator('target', 'hypothesis')
    @classmethod
    def meaningful(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError('Provide a specific target and possible counter-evidence.')
        return value
