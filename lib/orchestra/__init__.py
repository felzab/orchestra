"""orchestra: one program that keeps a fleet's state in files it alone writes.

Every verb ends with one of three exit codes, and refuses by raising `Refused`, whose message names
what to do next; a fault, which is no verdict, exits with a fourth.
"""

DONE = 0
# The verb did its work and something it reports still waits, such as an unclosed finding.
REMAINS = 1
REFUSED = 2
# A fault in the program rather than a verdict: sysexits.h's EX_SOFTWARE, past every code `land` adds.
FAULT = 70


class Refused(Exception):
    """A verb that judged nothing: its message says why and what to do next."""
