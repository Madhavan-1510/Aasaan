"""Errors the API turns into friendly 4xx responses."""


class UserError(Exception):
    """Bad request or missing data; surfaced as HTTP 400/404."""
