"""Owned-session adapters. Discovery never starts a model or reads credentials."""
from .ledger import Ledger, EffectUncertain, OwnershipError
