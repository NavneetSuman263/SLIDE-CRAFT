"""Basic import and route smoke test. Run with: python smoke_test.py"""
import app

client = app.app.test_client()
response = client.get("/")
assert response.status_code == 200, f"Landing page returned {response.status_code}"
response = client.get("/login")
assert response.status_code == 200, f"Login page returned {response.status_code}"
response = client.get("/dashboard")
assert response.status_code in (302, 401), f"Protected dashboard returned {response.status_code}"
print("SlideCraft smoke test passed: landing, login, and protected dashboard routes are responding.")
