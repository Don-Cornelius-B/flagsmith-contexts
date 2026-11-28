
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from flagsmith_contexts import ContextFlagsmith
from flagsmith_contexts.schemas import ContextLayer


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

API_URL = os.getenv(
    "FLAGSMITH_API_URL",
    "http://localhost:8000/api/v1/",
).rstrip("/") + "/"

# Client-side key: used by provisioning API endpoints.
PROVISION_ENVIRONMENT_KEY = os.getenv(
    "FLAGSMITH_ENVIRONMENT_KEY"
)

# Server-side key: used by the ContextFlagsmith SDK.
SDK_ENVIRONMENT_KEY = os.getenv(
    "FLAGSMITH_SERVER_KEY"
)

ADMIN_TOKEN = os.getenv("FLAGSMITH_ADMIN_TOKEN")

console = Console()


# ============================================================
# CONTEXT HIERARCHY
# ============================================================

org = ContextLayer(
    kind="org",
    id="acme_corp",
    traits={"plan": "enterprise"},
)

team = ContextLayer(
    kind="team",
    id="engineering",
    traits={"department": "engineering"},
)

user = ContextLayer(
    kind="user",
    id="don",
    traits={"role": "admin"},
)

contexts = [org, team, user]


# ============================================================
# FLAGSMITH ADMIN API CLIENT
# ============================================================

class FlagsmithAdmin:
    """Helper for Flagsmith OSS Admin API requests."""

    def __init__(self):
        if not ADMIN_TOKEN:
            raise RuntimeError(
                "FLAGSMITH_ADMIN_TOKEN is missing from .env."
            )

        self.http = httpx.Client(
            base_url=API_URL,
            headers={
                "Authorization": f"Api-Key {ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def request(self, method: str, path: str, **kwargs):
        response = self.http.request(method, path, **kwargs)

        console.print(
            f"[dim]{method} {path} -> "
            f"{response.status_code} "
            f"({response.headers.get('content-type', 'unknown')})[/dim]"
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Flagsmith API error: {response.status_code}\n"
                f"Endpoint: {path}\n"
                f"Response: {response.text[:1500]}"
            )

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Expected JSON from {path}.\n"
                f"Response: {response.text[:1000]}"
            ) from exc

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def close(self):
        self.http.close()


# ============================================================
# RESPONSE HELPERS
# ============================================================

def results_from(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data["results"]

        return [data]

    return []


# ============================================================
# PROJECT DISCOVERY
# ============================================================

def find_project_id(admin: FlagsmithAdmin) -> int:
    data = admin.get("projects/")
    projects = results_from(data)

    project = next(
        (
            item for item in projects
            if item.get("name") == "ToggleWars"
        ),
        None,
    )

    if not project:
        if len(projects) == 1:
            project = projects[0]
        else:
            raise RuntimeError(
                "Could not find the ToggleWars project. "
                "Check your Flagsmith Admin API token."
            )

    project_id = project.get("id")

    if not project_id:
        raise RuntimeError("Project ID was missing from the API response.")

    console.print(
        f"[green]Selected project: "
        f"{project.get('name')} (ID {project_id})[/green]"
    )

    return int(project_id)


# ============================================================
# FEATURE FLAGS
# ============================================================

def get_features(
    admin: FlagsmithAdmin,
    project_id: int,
) -> list[dict]:
    data = admin.get(f"projects/{project_id}/features/")
    return results_from(data)


def ensure_feature(
    admin: FlagsmithAdmin,
    project_id: int,
    name: str,
    feature_type: str = "STANDARD",
    default_enabled: bool = False,
) -> dict:

    features = get_features(admin, project_id)

    existing = next(
        (
            item for item in features
            if item.get("name") == name
        ),
        None,
    )

    payload = {
        "name": name,
        "type": feature_type,
        "default_enabled": default_enabled,
        "description": f"ToggleWars demo feature: {name}",
        "is_server_key_only": False,
    }

    if existing:
        console.print(
            f"[yellow]Reusing feature: {name}[/yellow]"
        )

        return admin.patch(
            f"projects/{project_id}/features/{existing['id']}/",
            json=payload,
        )

    console.print(f"[green]Creating feature: {name}[/green]")

    return admin.post(
        f"projects/{project_id}/features/",
        json=payload,
    )


# ============================================================
# ENTERPRISE SEGMENT
# ============================================================

def ensure_segment(
    admin: FlagsmithAdmin,
    project_id: int,
) -> dict:

    data = admin.get(f"projects/{project_id}/segments/")
    segments = results_from(data)

    existing = next(
        (
            item for item in segments
            if item.get("name") == "enterprise_plan"
        ),
        None,
    )

    payload = {
        "name": "enterprise_plan",
        "description": "Organizations on the enterprise plan",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "property": "plan",
                        "operator": "EQUAL",
                        "value": "enterprise",
                    }
                ],
            }
        ],
    }

    if existing:
        console.print(
            "[yellow]Reusing enterprise_plan segment[/yellow]"
        )

        return admin.patch(
            f"projects/{project_id}/segments/{existing['id']}/",
            json=payload,
        )

    console.print(
        "[green]Creating enterprise_plan segment[/green]"
    )

    return admin.post(
        f"projects/{project_id}/segments/",
        json=payload,
    )


# ============================================================
# SEGMENT OVERRIDE
# ============================================================

def ensure_segment_override(
    admin: FlagsmithAdmin,
    feature: dict,
    segment: dict,
):
    feature_id = feature["id"]
    segment_id = segment["id"]

    payload = {
        "enabled": True,
        "feature_state_value": {
            "type": "bool",
            "boolean_value": True,
        },
        "feature_segment": {
            "segment": segment_id,
            "priority": 0,
        },
    }

    try:
        admin.post(
            f"environments/{PROVISION_ENVIRONMENT_KEY}/"
            f"features/{feature_id}/create-segment-override/",
            json=payload,
        )

        console.print(
            "[green]Created enterprise segment override.[/green]"
        )

    except RuntimeError as exc:
        message = str(exc)

        if "400" in message or "409" in message:
            console.print(
                "[yellow]Override may already exist. "
                "Please verify it in Flagsmith.[/yellow]"
            )
        else:
            raise


# ============================================================
# MULTIVARIATE OPTIONS
# ============================================================

def ensure_multivariate_options(
    admin: FlagsmithAdmin,
    project_id: int,
    feature: dict,
) -> dict[str, int]:

    feature_id = feature["id"]

    detail = admin.get(
        f"projects/{project_id}/features/{feature_id}/"
    )

    existing_options = detail.get(
        "multivariate_options",
        [],
    )

    option_ids = {
        option.get("key"): option.get("id")
        for option in existing_options
        if option.get("key") and option.get("id")
    }

    desired = {
        "classic": "classic",
        "guided_ai": "guided_ai",
    }

    for key, value in desired.items():

        if key in option_ids:
            console.print(
                f"[yellow]Reusing variant: {key}[/yellow]"
            )
            continue

        payload = {
            "feature": feature_id,
            "type": "unicode",
            "string_value": value,
            "key": key,
            "default_percentage_allocation": 50,
        }

        created = admin.post(
            f"projects/{project_id}/features/"
            f"{feature_id}/mv-options/",
            json=payload,
        )

        option_ids[key] = created["id"]

        console.print(
            f"[green]Created variant: {key}[/green]"
        )

    missing = set(desired) - set(option_ids)

    if missing:
        raise RuntimeError(
            f"Missing experiment variants: {missing}"
        )

    return option_ids


# ============================================================
# EXPERIMENT
# ============================================================

def ensure_experiment(
    admin: FlagsmithAdmin,
    feature: dict,
    option_ids: dict[str, int],
):

    data = admin.get(
        f"environments/{PROVISION_ENVIRONMENT_KEY}/experiments/"
    )

    experiments = results_from(data)

    existing = next(
        (
            item for item in experiments
            if item.get("name") == "onboarding_flow"
        ),
        None,
    )

    if existing:
        console.print(
            "[yellow]Experiment onboarding_flow already exists. "
            "Reusing it.[/yellow]"
        )
        return existing

    payload = {
        "feature": feature["id"],
        "name": "onboarding_flow",
        "hypothesis": (
            "Guided AI onboarding improves user activation "
            "compared with classic onboarding."
        ),
        "experiment_rollout": {
            "enabled": True,
            "rollout_percentage": 100,
            "feature_state_value": {
                "type": "string",
                "value": "classic",
            },
            "multivariate_feature_state_values": [
                {
                    "multivariate_feature_option": option_ids["classic"],
                    "percentage_allocation": 50,
                },
                {
                    "multivariate_feature_option": option_ids["guided_ai"],
                    "percentage_allocation": 50,
                },
            ],
            "audience": {
                "segment_ids": [],
                "match": "all",
            },
        },
    }

    created = admin.post(
        f"environments/{PROVISION_ENVIRONMENT_KEY}/experiments/",
        json=payload,
    )

    console.print(
        "[green]Created onboarding_flow experiment.[/green]"
    )

    return created


# ============================================================
# PROVISIONING
# ============================================================

def provision():

    if not PROVISION_ENVIRONMENT_KEY:
        raise RuntimeError(
            "FLAGSMITH_ENVIRONMENT_KEY is missing from .env."
        )

    console.print(
        Panel(
            "[bold cyan]Flagsmith resource provisioning[/bold cyan]\n"
            "Creating or reusing ToggleWars resources.",
            border_style="cyan",
        )
    )

    admin = FlagsmithAdmin()

    try:
        project_id = find_project_id(admin)

        sso_feature = ensure_feature(
            admin,
            project_id,
            "sso_login",
            feature_type="STANDARD",
            default_enabled=False,
        )

        segment = ensure_segment(
            admin,
            project_id,
        )

        ensure_segment_override(
            admin,
            sso_feature,
            segment,
        )

        onboarding_feature = ensure_feature(
            admin,
            project_id,
            "onboarding_flow",
            feature_type="MULTIVARIATE",
            default_enabled=False,
        )

        option_ids = ensure_multivariate_options(
            admin,
            project_id,
            onboarding_feature,
        )

        ensure_experiment(
            admin,
            onboarding_feature,
            option_ids,
        )

        console.print(
            Panel(
                "[bold green]Provisioning requests completed.[/bold green]\n"
                "Review the resources and experiment in Flagsmith.",
                border_style="green",
            )
        )

    finally:
        admin.close()


# ============================================================
# DISPLAY CONTEXT HIERARCHY
# ============================================================

def display_hierarchy():

    table = Table(
        title="Organization → Team → User",
        header_style="bold cyan",
    )

    table.add_column("Level", style="yellow")
    table.add_column("Identity", style="green")
    table.add_column("Traits")

    table.add_row(
        "Organization",
        "acme_corp",
        "plan=enterprise",
    )

    table.add_row(
        "Team",
        "engineering",
        "department=engineering",
    )

    table.add_row(
        "User",
        "don",
        "role=admin",
    )

    console.print(table)


# ============================================================
# HIERARCHICAL EVALUATION AND CACHE
# ============================================================

def evaluate_hierarchy(client: ContextFlagsmith):

    result = client.get_context_flags(
        contexts=contexts,
        bucketing_context="org",
        include_baseline=True,
    )

    table = Table(
        title="Resolved Feature Flags",
        header_style="bold cyan",
    )

    for column in (
        "Feature",
        "Enabled",
        "Value",
        "Resolved By",
        "Reason",
    ):
        table.add_column(column)

    if not result.flags:
        console.print(
            "[yellow]No feature flags returned.[/yellow]"
        )

    for name, flag in result.flags.items():
        table.add_row(
            str(name),
            str(flag.enabled),
            str(flag.value),
            str(flag.resolved_by),
            str(flag.reason),
        )

    if result.flags:
        console.print(table)

    meta = result.meta

    console.print(
        f"[cyan]Cache hits:[/cyan] {meta.cache_hits}  "
        f"[cyan]Cache misses:[/cyan] {meta.cache_misses}  "
        f"[cyan]Evaluation:[/cyan] {meta.evaluation_ms:.2f} ms"
    )

    return result


# ============================================================
# ACCOUNT-LEVEL EXPERIMENT ASSIGNMENT
# ============================================================

def evaluate_experiment(client: ContextFlagsmith):

    console.print(
        "\n[bold cyan]ACCOUNT-LEVEL EXPERIMENT ASSIGNMENT[/bold cyan]"
    )

    try:
        result = client.sdk_client.get_experiment_flag(
            feature_name="onboarding_flow",
            identifier=org.namespaced_id,
            traits=dict(org.traits),
        )

        table = Table(
            title="Onboarding Experiment",
        )

        table.add_column("Property", style="yellow")
        table.add_column("Result", style="green")

        table.add_row(
            "Feature",
            str(result.feature_name),
        )

        table.add_row(
            "Variant",
            str(result.variant),
        )

        console.print(table)

        return result

    except Exception as exc:
        console.print(
            f"[yellow]Experiment evaluation failed: {exc}[/yellow]"
        )
        return None


# ============================================================
# CONVERSION EVENT
# ============================================================

def track_conversion(client: ContextFlagsmith):

    console.print(
        "\n[bold cyan]CONVERSION EVENT TRACKING[/bold cyan]"
    )

    result = client.track_context_event(
        event_name="onboarding_completed",
        contexts=contexts,
        bucketing_context="org",
        value=1,
        metadata={
            "source": "togglewars_demo",
            "experiment": "onboarding_flow",
        },
    )

    table = Table(
        title="Conversion Event",
    )

    table.add_column("Property", style="yellow")
    table.add_column("Result", style="green")

    table.add_row("Status", str(result.status))
    table.add_row("Event", str(result.event_name))
    table.add_row("Attributed To", str(result.attributed_to))

    console.print(table)


# ============================================================
# SIMULATION
# ============================================================

def simulate():

    if not SDK_ENVIRONMENT_KEY:
        raise RuntimeError(
            "FLAGSMITH_SERVER_KEY is missing from .env. "
            "The simulation requires a server-side environment key."
        )

    client = ContextFlagsmith(
        environment_key=SDK_ENVIRONMENT_KEY,
        api_url=API_URL,
    )

    console.print(
        Panel(
            "[bold cyan]ToggleWars ContextFlagsmith Demo[/bold cyan]\n"
            "Hierarchical evaluation, caching, experimentation "
            "and conversion tracking.",
            border_style="cyan",
        )
    )

    display_hierarchy()

    if not client.ping():
        raise RuntimeError(
            "Cannot connect to Flagsmith. "
            "Check Docker and your API URL."
        )

    console.print(
        "\n[bold green]Connected to Flagsmith.[/bold green]"
    )

    console.print(
        "\n[bold yellow]FIRST EVALUATION[/bold yellow]"
    )

    evaluate_hierarchy(client)

    console.print(
        "\n[bold yellow]SECOND EVALUATION (CACHE)[/bold yellow]"
    )

    evaluate_hierarchy(client)

    evaluate_experiment(client)

    track_conversion(client)

    console.print(
        "\n[bold green]Simulation completed.[/bold green]"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Provision and simulate the ToggleWars demo."
    )

    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Skip provisioning and run only the simulation.",
    )

    args = parser.parse_args()

    try:
        if not args.simulate:
            provision()

        simulate()

    except Exception as exc:
        console.print(
            f"\n[bold red]Demo failed:[/bold red] {exc}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
