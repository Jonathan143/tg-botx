"""Application composition helpers.

Keep object construction in this package so CLI, API and future workers share
the same wiring without importing one another.  The container is imported
explicitly to keep package imports lightweight and cycle-free.
"""
