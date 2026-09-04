"""
Standalone connectivity check: confirms the SAP HANA cert (hana_server.pem)
validates correctly before you run the full bot.

Run with:  python test_connection.py
"""

from hdbcli import dbapi

from modules import config

try:
    conn = dbapi.connect(**config.HANA_CONFIG)
    print("Connected successfully with validated cert.")
    conn.close()
except Exception as e:
    print(f"Connection failed: {e}")