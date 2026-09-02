import base64

with open("hana_server.cer", "rb") as f:
    der_bytes = f.read()

b64 = base64.b64encode(der_bytes).decode("ascii")

# Wrap at 64 chars per line, per PEM spec
lines = [b64[i:i+64] for i in range(0, len(b64), 64)]

pem = "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"

with open("hana_server.pem", "w") as f:
    f.write(pem)

print("Wrote hana_server.pem")
