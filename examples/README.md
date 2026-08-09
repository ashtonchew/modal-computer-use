# Examples

## Primary path

Start with [`modal_function_session_handoff.py`](modal_function_session_handoff.py). It shows the
complete optimized default:

- one native async owner creates the desktop;
- one versioned handle crosses into an application-owned, exactly placed Modal Function;
- one borrow surrounds the repeated model trajectory;
- one pooled async HTTP client carries each `computer.step()` request;
- each step sends one ordered action batch and returns one typed immediate post-action screenshot;
- the borrower releases its lease before the owner cleans up.

The OpenAI and Anthropic examples keep their model loops in application code and use the same
handoff contract. They do not add either provider SDK to core.

## Low-level compatibility

Use the other examples for a specific primitive or operating task:

| Need | Example |
| --- | --- |
| Direct local daemon control | [`01_mouse_keyboard_screenshots.py`](01_mouse_keyboard_screenshots.py) |
| Attach without lifecycle ownership | [`attach_existing_sandbox.py`](attach_existing_sandbox.py) |
| Own an intentionally unplaced async desktop | [`async_modal_owner.py`](async_modal_owner.py) |
| Named desktop acquisition | [`async_named_desktop.py`](async_named_desktop.py) |
| Explicit warm-pool capacity | [`04_warm_pool.py`](04_warm_pool.py) |
| Recording lifecycle | [`recording_lifecycle.py`](recording_lifecycle.py) |
| Volume-backed artifacts | [`volume_artifacts.py`](volume_artifacts.py) |
| Recovery-oriented run gateway | [`modal_run_gateway.py`](modal_run_gateway.py) |

These examples remain supported. They do not replace the complete placed trajectory as the primary
SDK path.
