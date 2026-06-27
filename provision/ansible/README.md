# provision/ansible

One playbook provisions all 13 nodes: lays down the code, configures the render
and control roles, and applies field hardening (overlayfs read-only root,
watchdog, volatile logs).

## Prerequisites

- A control machine (your laptop or pi-13) with Ansible and the `community.general`
  + `ansible.posix` collections:
  ```bash
  pip install ansible
  ansible-galaxy collection install community.general ansible.posix
  ```
- SSH access to each Pi as user `pi` (key-based), Pi OS Bookworm flashed,
  static IPs per `inventory.ini`.

## Usage

```bash
cd provision/ansible
ansible-playbook site.yml                                  # all 13
ansible-playbook site.yml --limit 'pi-01,pi-02,pi-13'      # first two-node install
ansible-playbook site.yml --limit pi-07                    # one swapped node
ansible-playbook site.yml --tags hardening                 # re-apply hardening only
```

See `docs/HARDENING.md` for the cold-spare recovery flow and how to open/re-seal
a node's read-only root.

## Layout

```
site.yml            three plays: common -> {render, control} -> hardening (last)
inventory.ini       pi-01..pi-12 (render), pi-13 (control)
group_vars/all.yml  control host, ports, preview cadence, overlayfs/watchdog toggles
roles/common        base packages, repo sync, hostname, console boot
roles/render        GL/KMS + decode deps, config.json, render.service, KMS boot config
roles/control       clock + controller services, room-model git
roles/hardening     overlayfs read-only root, watchdog, volatile logs
```
