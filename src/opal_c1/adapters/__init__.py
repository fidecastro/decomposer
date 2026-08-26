"""Adapters: where the ports meet the actual machine.

Everything here is allowed to do IO — that is its job. The rule is the
inverse of the core's: adapters may import hardware SDKs and the core, but
application logic (deciding *whether* and *when*) stays out of them.
"""
