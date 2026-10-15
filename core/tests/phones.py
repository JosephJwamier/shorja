"""Distinct, valid Iraqi mobile numbers for test fixtures.

User.phone is unique now, so a fixture that hard-codes one number works for
the first user it builds and raises IntegrityError on the second. It also
has to be a *valid* number: User.save() refuses anything to_e164() cannot
parse, so the old '0770' + '0'*6 (nine national digits) would now raise
ValueError rather than being quietly stored.

next_phone() hands out a fresh valid number per call. Prefer it over a
literal in any fixture that builds more than one user.
"""
from itertools import count

_seq = count(1)


def next_phone():
    """'07700000001', '07700000002', ... — eleven digits, prefix 77."""
    return f'0770{next(_seq):07d}'
