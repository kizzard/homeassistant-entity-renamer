#!/usr/bin/env python3

import argparse
import config
import csv
import json
import re
import requests
import ssl
import tabulate
import urllib.parse
import websocket

tabulate.PRESERVE_WHITESPACE = True

# Determine the protocol based on TLS configuration
TLS_S = 's' if config.TLS else ''

# Header containing the access token
headers = {
    'Authorization': f'Bearer {config.ACCESS_TOKEN}',
    'Content-Type': 'application/json'
}


def align_strings(table):
    alignment_char = "."

    if len(table) == 0:
        return

    for column in range(len(table[0])):
        column_data = [row[column] for row in table]
        strings_to_align = [s for s in column_data if alignment_char in s]
        if len(strings_to_align) == 0:
            continue

        max_length = max([len(s.split(alignment_char)[0]) for s in strings_to_align])

        def align_string(s):
            s_split = s.split(alignment_char, maxsplit=1)
            if len(s_split) == 1:
                return s
            return f"{s_split[0]:>{max_length}}.{s_split[1]}"

        table = [
            tuple(align_string(value) if i == column else value for i, value in enumerate(row))
            for row in table
        ]

    return table


class HomeAssistantClient:
    def __init__(self):
        self.api_base_url = f'http{TLS_S}://{config.HOST}'
        self.websocket_url = f'ws{TLS_S}://{config.HOST}/api/websocket'
        self.ws = None
        self.next_ws_id = 1

    def request(self, method, path, payload=None, expected_status_codes=None):
        if expected_status_codes is None:
            expected_status_codes = [200]

        response = requests.request(
            method,
            f'{self.api_base_url}{path}',
            headers=headers,
            verify=config.SSL_VERIFY,
            json=payload
        )

        if response.status_code not in expected_status_codes:
            raise RuntimeError(f"{method} {path} failed: {response.status_code} - {response.text}")

        return response

    def connect_websocket(self):
        if self.ws is not None:
            return self.ws

        sslopt = {"cert_reqs": ssl.CERT_NONE} if not config.SSL_VERIFY else {}
        self.ws = websocket.WebSocket(sslopt=sslopt)
        self.ws.connect(self.websocket_url)

        auth_required = json.loads(self.ws.recv())
        if auth_required.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected websocket response: {auth_required}")

        auth_msg = json.dumps({"type": "auth", "access_token": config.ACCESS_TOKEN})
        self.ws.send(auth_msg)

        auth_result = json.loads(self.ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError("Authentication failed. Check your access token.")

        return self.ws

    def ws_request(self, payload):
        ws = self.connect_websocket()
        message_id = self.next_ws_id
        self.next_ws_id += 1

        ws.send(json.dumps(dict(payload, id=message_id)))

        while True:
            response = json.loads(ws.recv())

            if response.get("id") != message_id or response.get("type") != "result":
                continue

            if not response.get("success"):
                error = response.get("error", {})
                raise RuntimeError(error.get("message", str(response)))

            return response.get("result")

    def close(self):
        if self.ws is not None:
            self.ws.close()
            self.ws = None


def list_entities(regex=None):
    response = requests.get(
        f'http{TLS_S}://{config.HOST}/api/states',
        headers=headers,
        verify=config.SSL_VERIFY
    )

    if response.status_code != 200:
        raise RuntimeError(f"GET /api/states failed: {response.status_code} - {response.text}")

    data = response.json()

    entity_data = [(entity['attributes'].get('friendly_name', ''), entity['entity_id']) for entity in data]

    if regex:
        entity_data = [
            (friendly_name, entity_id)
            for friendly_name, entity_id in entity_data
            if re.search(regex, entity_id)
        ]

    return sorted(entity_data, key=lambda x: x[0])


def build_rename_data(entity_data, search_regex, replace_regex=None):
    rename_data = []
    if replace_regex is not None:
        for friendly_name, entity_id in entity_data:
            new_entity_id = re.sub(search_regex, replace_regex, entity_id)
            rename_data.append((friendly_name, entity_id, new_entity_id))
    else:
        rename_data = [(friendly_name, entity_id, "") for friendly_name, entity_id in entity_data]

    return rename_data


def print_rename_preview(rename_data, output_csv=None):
    table = [("Friendly Name", "Current Entity ID", "New Entity ID")] + align_strings(rename_data)
    print(tabulate.tabulate(table, headers="firstrow", tablefmt="github"))

    if output_csv:
        csv_table = [("Friendly Name", "Current Entity ID", "New Entity ID")] + rename_data
        with open(output_csv, 'w', newline='') as csvfile:
            csv_writer = csv.writer(csvfile)
            csv_writer.writerows(csv_table)
            print(f"(Table written to {output_csv})")


def replace_entity_references(value, entity_id_map):
    if isinstance(value, str):
        new_value = entity_id_map.get(value, value)
        return new_value, int(new_value != value)

    if isinstance(value, list):
        updated_items = []
        replacements = 0
        changed = False

        for item in value:
            new_item, item_replacements = replace_entity_references(item, entity_id_map)
            updated_items.append(new_item)
            replacements += item_replacements
            changed = changed or item_replacements > 0

        return (updated_items if changed else value), replacements

    if isinstance(value, dict):
        updated_items = {}
        replacements = 0
        changed = False

        for key, item in value.items():
            new_key, key_replacements = replace_entity_references(key, entity_id_map)
            new_item, item_replacements = replace_entity_references(item, entity_id_map)
            updated_items[new_key] = new_item
            replacements += key_replacements + item_replacements
            changed = changed or key_replacements > 0 or item_replacements > 0

        return (updated_items if changed else value), replacements

    return value, 0


def build_reference_update(reference_type, identifier, label, config_data, save_path=None, save_ws_type=None, save_ws_url_path=None):
    updated_config, replacements = replace_entity_references(config_data, identifier["entity_id_map"])
    if replacements == 0:
        return None

    return {
        "reference_type": reference_type,
        "config_key": identifier.get("config_key"),
        "label": label,
        "replacements": replacements,
        "config": updated_config,
        "save_path": save_path,
        "save_ws_type": save_ws_type,
        "save_ws_url_path": save_ws_url_path,
    }


def discover_reference_updates(client, entity_id_map):
    registry_entries = client.ws_request({"type": "config/entity_registry/list"})
    updates = []
    identifier = {"entity_id_map": entity_id_map}

    for entry in registry_entries:
        entity_id = entry.get("entity_id")
        unique_id = entry.get("unique_id")

        if entity_id is None or unique_id is None:
            continue

        if entity_id.startswith("automation."):
            config = client.ws_request({"type": "automation/config", "entity_id": entity_id}).get("config")
            update = build_reference_update(
                "automation",
                dict(identifier, config_key=unique_id),
                entity_id,
                config,
                save_path=f"/api/config/automation/config/{urllib.parse.quote(unique_id, safe='')}"
            )
            if update:
                updates.append(update)

        elif entity_id.startswith("script."):
            config = client.ws_request({"type": "script/config", "entity_id": entity_id}).get("config")
            update = build_reference_update(
                "script",
                dict(identifier, config_key=unique_id),
                entity_id,
                config,
                save_path=f"/api/config/script/config/{urllib.parse.quote(unique_id, safe='')}"
            )
            if update:
                updates.append(update)

        elif entity_id.startswith("scene."):
            scene_path = f"/api/config/scene/config/{urllib.parse.quote(unique_id, safe='')}"
            response = client.request('GET', scene_path, expected_status_codes=[200, 404])
            if response.status_code == 404:
                continue

            update = build_reference_update(
                "scene",
                dict(identifier, config_key=unique_id),
                entity_id,
                response.json(),
                save_path=scene_path
            )
            if update:
                updates.append(update)

    for dashboard in client.ws_request({"type": "lovelace/dashboards/list"}):
        if dashboard.get("mode") != "storage":
            continue

        url_path = dashboard.get("url_path")
        payload = {"type": "lovelace/config"}
        if url_path is not None:
            payload["url_path"] = url_path

        config = client.ws_request(payload)
        update = build_reference_update(
            "dashboard",
            identifier,
            dashboard.get("title", url_path or "Default dashboard"),
            config,
            save_ws_type="lovelace/config/save",
            save_ws_url_path=url_path
        )
        if update:
            updates.append(update)

    return updates


def print_reference_update_preview(reference_updates):
    if not reference_updates:
        print("\nNo references found in automations, scripts, scenes, or storage dashboards.")
        return

    table = [("Type", "Target", "References Updated")]
    for update in reference_updates:
        table.append((update["reference_type"], update["label"], str(update["replacements"])))

    print("\nReferences that will also be updated:")
    print(tabulate.tabulate(table, headers="firstrow", tablefmt="github"))
    print("\nNote: automatic reference updates currently cover automations, scripts, scenes, and storage dashboards.")


def rename_entities(client, rename_data):
    for friendly_name, entity_id, new_entity_id in rename_data:
        if entity_id == new_entity_id:
            print(f"Entity '{entity_id}' already matches the requested name; skipping.")
            continue

        client.ws_request({
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
            "new_entity_id": new_entity_id
        })
        print(f"Entity '{entity_id}' renamed to '{new_entity_id}' successfully!")


def apply_reference_updates(client, reference_updates):
    if not reference_updates:
        return

    print("\nUpdating references to the renamed entities...")

    for update in reference_updates:
        if update["save_path"] is not None:
            client.request('POST', update["save_path"], payload=update["config"])
        else:
            payload = {
                "type": update["save_ws_type"],
                "config": update["config"],
            }
            if update["save_ws_url_path"] is not None:
                payload["url_path"] = update["save_ws_url_path"]
            client.ws_request(payload)

        print(
            f"Updated {update['replacements']} reference(s) in "
            f"{update['reference_type']} '{update['label']}'."
        )


def process_entities(entity_data, search_regex, replace_regex=None, output_csv=None, update_references=True):
    rename_data = build_rename_data(entity_data, search_regex, replace_regex)
    print_rename_preview(rename_data, output_csv)

    if replace_regex is None:
        return

    rename_operations = [row for row in rename_data if row[1] != row[2]]
    if not rename_operations:
        print("\nNo entity IDs would change.")
        return

    reference_updates = []
    client = HomeAssistantClient()

    try:
        if update_references:
            entity_id_map = {current_entity_id: new_entity_id for _, current_entity_id, new_entity_id in rename_operations}
            reference_updates = discover_reference_updates(client, entity_id_map)
            print_reference_update_preview(reference_updates)

        answer = input("\nDo you want to proceed with renaming the entities? (y/N): ")
        if answer.lower() not in ["y", "yes"]:
            print("Renaming process aborted.")
            return

        rename_entities(client, rename_operations)

        if update_references:
            apply_reference_updates(client, reference_updates)

    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HomeAssistant Entity Renamer")
    parser.add_argument('--search', dest='search_regex', help='Regular expression for search. Note: Only searches entity IDs.')
    parser.add_argument('--replace', dest='replace_regex', help='Regular expression for replace')
    parser.add_argument('--output-csv', dest='output_csv', help='Output preview table to CSV.')
    parser.add_argument(
        '--skip-reference-updates',
        dest='skip_reference_updates',
        action='store_true',
        help='Only rename the entity IDs themselves and do not update automation/script/scene/dashboard references.'
    )
    args = parser.parse_args()

    if args.search_regex:
        entity_data = list_entities(args.search_regex)

        if entity_data:
            process_entities(
                entity_data,
                args.search_regex,
                args.replace_regex,
                args.output_csv,
                update_references=not args.skip_reference_updates
            )
        else:
            print("No entities found matching the search regex.")
    else:
        parser.print_help()
