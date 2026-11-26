import inspect
import json
import os
import sys
from pathlib import Path
import httpx
from flagsmith import Flagsmith

API_URL = "http://localhost:8000/api/v1"
SERVER_KEY = os.environ.get("FLAGSMITH_SERVER_KEY") or (sys.argv[1] if len(sys.argv) > 1 else "")

if not SERVER_KEY.startswith("ser."):
    print("Usage: python scripts/verify_flagsmith_contract.py ser.<your_key>")
    sys.exit(1)

fixtures_dir = Path("tests/fixtures")
fixtures_dir.mkdir(parents=True, exist_ok=True)
headers = {"X-Environment-Key": SERVER_KEY, "Content-Type": "application/json"}

# 1. Capture raw GET /api/v1/flags/ (Global Baseline)
flags_resp = httpx.get(f"{API_URL}/flags/", headers=headers, timeout=10.0)
flags_resp.raise_for_status()
(fixtures_dir / "real_environment_flags.json").write_text(json.dumps(flags_resp.json(), indent=2))
print("[OK] Saved tests/fixtures/real_environment_flags.json")

# 2. Capture raw POST /api/v1/identities/ (Org layer matching enterprise_plan segment)
org_payload = {
    "identifier": "ctx:org:acme_corp",
    "traits": [{"trait_key": "plan", "trait_value": "enterprise", "transient": True}],
    "transient": True,
}
org_resp = httpx.post(f"{API_URL}/identities/", headers=headers, json=org_payload, timeout=10.0)
org_resp.raise_for_status()
(fixtures_dir / "real_org_identity.json").write_text(json.dumps(org_resp.json(), indent=2))
print("[OK] Saved tests/fixtures/real_org_identity.json")

# 3. Capture raw POST /api/v1/identities/ (User layer with no overrides -> DEFAULT)
user_payload = {
    "identifier": "ctx:user:don",
    "traits": [{"trait_key": "role", "trait_value": "admin", "transient": True}],
    "transient": True,
}
user_resp = httpx.post(f"{API_URL}/identities/", headers=headers, json=user_payload, timeout=10.0)
user_resp.raise_for_status()
(fixtures_dir / "real_user_identity.json").write_text(json.dumps(user_resp.json(), indent=2))
print("[OK] Saved tests/fixtures/real_user_identity.json")

# 4. Verify Python SDK evaluation & print exact Experimentation method signatures
client = Flagsmith(environment_key=SERVER_KEY, api_url=f"{API_URL}/")
sdk_org = client.get_identity_flags(identifier="ctx:org:acme_corp", traits={"plan": "enterprise"})
sdk_user = client.get_identity_flags(identifier="ctx:user:don", traits={"role": "admin"})

print("\n--- SDK Evaluation Results ---")
for label, flag_set in [("ORG (acme_corp)", sdk_org), ("USER (don)", sdk_user)]:
    f = flag_set.get_flag("sso_login")
    print(f"{label}: enabled={f.enabled}, value={f.value}, reason={getattr(f, 'reason', None)}")

print("\n--- Installed Flagsmith SDK Signatures ---")
print("Flagsmith.__init__:", inspect.signature(Flagsmith.__init__))
for method_name in ["get_environment_flags", "get_identity_flags", "get_experiment_flag", "track_event", "track_exposure_event"]:
    if hasattr(Flagsmith, method_name):
        print(f"Flagsmith.{method_name}:", inspect.signature(getattr(Flagsmith, method_name)))
