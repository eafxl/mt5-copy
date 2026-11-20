"""Test script to check terminal connections."""
import MetaTrader5 as mt5
import time

MASTER_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SLAVE_PATH = r"C:\Program Files\MetaTrader 5 Slave\terminal64.exe"

print("=" * 50)
print("MT5 Terminal Connection Test")
print("=" * 50)

# Test 1: Connect to master
print("\n[1] Testing MASTER terminal connection...")
print(f"    Path: {MASTER_PATH}")

if mt5.initialize(path=MASTER_PATH, timeout=30000):
    info = mt5.account_info()
    if info:
        print(f"    SUCCESS! Connected to account {info.login}")
    else:
        print(f"    Connected but no account info")
    mt5.shutdown()
    print("    Disconnected from master")
else:
    print(f"    FAILED: {mt5.last_error()}")

time.sleep(2)

# Test 2: Connect to slave
print("\n[2] Testing SLAVE terminal connection...")
print(f"    Path: {SLAVE_PATH}")

if mt5.initialize(path=SLAVE_PATH, timeout=60000):
    info = mt5.account_info()
    if info:
        print(f"    SUCCESS! Connected to account {info.login}")
    else:
        print(f"    Connected but no account logged in")
        print("    You need to login to slave account in the terminal first!")
    mt5.shutdown()
    print("    Disconnected from slave")
else:
    print(f"    FAILED: {mt5.last_error()}")

print("\n" + "=" * 50)
print("Test complete!")
print("=" * 50)
input("Press Enter to exit...")
