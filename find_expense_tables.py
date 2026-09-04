"""
Broader expense-record search: covers option 1 (AP Invoices against a
vendor) and option 3 (a custom User-Defined Table built for expense
claims), since OEXD/AEXD turned out to be expense-TYPE setup data, not
actual transactions.

Run with:  python find_expense_records_v2.py
"""

from hdbcli import dbapi
from modules import config

conn = dbapi.connect(**config.HANA_CONFIG)
cursor = conn.cursor()
schema = config.HANA_CONFIG["currentSchema"]

print(f"Connected. Schema: {schema}\n")

# ==========================================
# 1. Look for custom User-Defined Tables (UDTs)
# ==========================================
# SAP B1 UDTs are physically stored as "@TABLENAME". OUTB is the B1
# system catalog of registered UDTs (TableID/TableName/TableType),
# which is more reliable than guessing at SYS.TABLE_COLUMNS name
# patterns since UDT naming is fully company-specific.
print("=" * 60)
print("STEP 1: Registered User-Defined Tables (UDTs)")
print("=" * 60)
try:
    cursor.execute('SELECT "TableID", "TableName", "TableType" FROM "OUTB" ORDER BY "TableName"')
    udts = cursor.fetchall()
    if not udts:
        print("No UDTs registered in OUTB.")
    else:
        for table_id, table_name, table_type in udts:
            flag = "  <-- looks expense/claim related" if any(
                kw in (table_name or "").upper() for kw in
                ["EXP", "CLAIM", "REIMB", "OT", "MEAL", "PETTY"]
            ) else ""
            print(f"  @{table_id}  ({table_name})  type={table_type}{flag}")
except Exception as e:
    print(f"Could not read OUTB: {e}")

print()

# ==========================================
# 2. Vendor master: any catering/restaurant/food vendors?
# ==========================================
print("=" * 60)
print("STEP 2: Vendors (OCRD) with catering/restaurant/food-like names")
print("=" * 60)
sql = """
    SELECT "CardCode", "CardName", "CardType"
    FROM "OCRD"
    WHERE "CardType" = 'S'
      AND (UPPER("CardName") LIKE '%CATER%'
        OR UPPER("CardName") LIKE '%RESTAURANT%'
        OR UPPER("CardName") LIKE '%FOOD%'
        OR UPPER("CardName") LIKE '%CAFE%'
        OR UPPER("CardName") LIKE '%KITCHEN%'
        OR UPPER("CardName") LIKE '%MEAL%')
    ORDER BY "CardName"
"""
cursor.execute(sql)
vendors = cursor.fetchall()
if not vendors:
    print("No vendors matched food/catering-like names.")
else:
    for code, name, ctype in vendors:
        print(f"  {code}  {name}")

print()

# ==========================================
# 3. AP Invoices (OPCH/PCH1) with meal-related line descriptions, 2026
# ==========================================
print("=" * 60)
print("STEP 3: AP Invoice lines (PCH1) mentioning meals/food, 2026")
print("=" * 60)
sql = """
    SELECT T0."DocNum", T0."CardName", T0."DocDate", T1."Dscription", T1."LineTotal"
    FROM "OPCH" T0
    INNER JOIN "PCH1" T1 ON T0."DocEntry" = T1."DocEntry"
    WHERE T0."DocDate" >= '2026-01-01' AND T0."DocDate" <= '2026-12-31'
      AND (UPPER(T1."Dscription") LIKE '%MEAL%'
        OR UPPER(T1."Dscription") LIKE '%FOOD%'
        OR UPPER(T1."Dscription") LIKE '%CATERING%'
        OR UPPER(T1."Dscription") LIKE '%OVERTIME%'
        OR UPPER(T1."Dscription") LIKE '%OT %'
        OR UPPER(T0."Comments") LIKE '%MEAL%'
        OR UPPER(T0."Comments") LIKE '%OVERTIME%')
    ORDER BY T0."DocDate" DESC
"""
try:
    cursor.execute(sql)
    rows = cursor.fetchmany(30)
    if not rows:
        print("No matching AP invoice lines found for 2026.")
    else:
        for docnum, cardname, docdate, dscription, linetotal in rows:
            print(f"  DocNum {docnum} | {cardname} | {docdate} | {dscription} | {linetotal}")
except Exception as e:
    print(f"Query failed: {e}")

cursor.close()
conn.close()
print("\nDone.")