"""Bank adapters, behind one interface.

`csv_import` is the offline path and where testing starts; `starling` is the
live one. Monzo goes here too when it is wanted — one file, implementing
`BankAdapter`, with nothing above this package needing to change.
"""

from .base import Balance, BankAdapter, SupportsTransfers
from .csv_import import CsvAdapter, parse
from .starling import StarlingAdapter

__all__ = [
    "Balance",
    "BankAdapter",
    "CsvAdapter",
    "StarlingAdapter",
    "SupportsTransfers",
    "parse",
]
