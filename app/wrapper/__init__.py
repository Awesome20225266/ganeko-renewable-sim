"""Restricted, user-facing wrapper around the external Renewable Generation API.

Exposes only LIVE + HISTORICAL data (never FORECAST) under /api/renewable/*.
Calls the provider server-side with a secret key that is never shown to users.
"""
