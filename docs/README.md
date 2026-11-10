# Documentation

## Using it

- [setup.md](setup.md): redirecting the controller's traffic (firewall NAT rule or DNS rewrite), deploying with Portainer / compose
- [web-ui.md](web-ui.md): monitor, raw registers, heating settings and log download pages
- [operating-modes.md](operating-modes.md): `proxy`, `hybrid` and `local` mode, how writes work, write API
- [home-assistant.md](home-assistant.md): MQTT publisher with Home Assistant discovery
- [cloud-monitoring.md](cloud-monitoring.md): polling the Estyma cloud next to the local data
- [configuration.md](configuration.md): every environment variable *(generated)*

## Reference

- [registers.md](registers.md): every known controller register with encoding and range *(generated)*
- [register-mapping.md](register-mapping.md): how cloud fields were matched to registers (analysis results)
- [cloud-translation.md](cloud-translation.md): the cloud's Polish field names, their meaning and wrapper bugs

## Plans and development

- [home-assistant-roadmap.md](home-assistant-roadmap.md): phases towards a full local server and Home Assistant control
- [development.md](development.md): code layout, generated files, tests, image build, log format

Files marked *generated* are built by `tools/gendocs.py` from `proxy/catalog.py`
and `proxy/config.py`; edit those, not the Markdown.
