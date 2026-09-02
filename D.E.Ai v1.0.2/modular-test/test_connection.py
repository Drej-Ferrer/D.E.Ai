from hdbcli import dbapi
from dotenv import load_dotenv
import os

load_dotenv("lark-hana.env", override=True)

config = {
    "address": os.getenv("SAP_HOST"),
    "port": int(os.getenv("SAP_PORT", 30015)),
    "user": os.getenv("SAP_USER"),
    "password": os.getenv("SAP_PASSWORD"),
    "currentSchema": os.getenv("SAP_SCHEMA"),
    "encrypt": True,
    "sslValidateCertificate": True,
    "sslHostNameInCertificate": "DIRECHANASERVER"
}

try:
    conn = dbapi.connect(**config)
    print("Connected successfully with validated cert.")
    conn.close()
except Exception as e:
    print(f"Connection failed: {e}")
