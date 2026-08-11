# External provider action-to-frame benchmark, 2026-08-11

**Evidence status:** eligible

This report measures one click followed by the next full screenshot through four public SDK paths. The paths use different caller topologies and screenshot formats. Read the values as complete path measurements. A provider ranking needs a separate campaign with matched configurations.

## Results

All arms use the same action case and timer boundary. Each arm reports its screenshot representation, warmup count, and measured count.

| Case | Path | p50 (ms) | p95 (ms) | n | Status |
| --- | --- | ---: | ---: | ---: | --- |
| ordered-actions-to-immediate-frame-v1 | modal-daemon / computer.step | 43.13 | 46.35 | 100 | measured |
| ordered-actions-to-immediate-frame-v1 | daytona / provider-sdk-action-then-screenshot | 1039.59 | 1143.06 | 100 | measured |
| ordered-actions-to-immediate-frame-v1 | e2b / provider-sdk-action-then-screenshot | 15659.63 | 15744.76 | 100 | measured |
| ordered-actions-to-immediate-frame-v1 | tzafon / provider-sdk-action-then-screenshot | 264.37 | 346.10 | 100 | measured |

## Method

Case: `ordered-actions-to-immediate-frame-v1`.
Action semantics: `one-left-click-at-512-384-then-immediate-full-frame`.
Timer boundary: `caller_before_ordered_action_dispatch_to_validated_immediate_full_frame_bytes`.
Warmup iterations: 2. Measured iterations: 100.
Screenshot policy: `provider-native-full-frame`.
Action payload SHA-256: `83599900ae670680c7d84271000b03114940c492d935c26b5f0999a281958296`.

## Configuration

| Provider | SDK | SDK retry policy | Caller | Requested region | Observed region | Screenshot | CPU | Memory (MiB) | SDK calls | Transport requests |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| modal-daemon | modal-computer-use 2.0.0 | no-mutation-retry | application-owned-modal-function | us-west-2 | us-west-2 | PNG dimensions unknown cursor=false | 1.0 | 2048 | 1 | 1 |
| daytona | daytona 0.175.0 | provider-default | external-provider-sdk-caller | provider-default | provider-default | PNG 1024x768 cursor=unknown | 1.0 | 1024 | 2 | 2 |
| e2b | e2b-desktop 2.4.2 | provider-default | external-provider-sdk-caller | provider-default | provider-default | PNG 1024x768 cursor=unknown | 2.0 | 1024 | 2 | 3 |
| tzafon | tzafon 2.44.1 | provider-default | external-provider-sdk-caller | provider-default | provider-default | JPEG 1280x720 cursor=unknown | Not disclosed | Not disclosed | 2 | 2 |

## Evidence

Source SHA: `767429c0c4b074cbdb6461767d9090ec3090b3bd`.

Input artifact digests:

- modal-daemon / cleanup_verification: `ecb4ba3001c71212fefe3795b43018345595301d5f6b63446a0b98409fadc597`
- modal-daemon / provider_compare: `8291db81299c8cd4892a99ddf795b199566506cde613d8109ddca01294209b06`
- modal-daemon / step_candidate: `e0d2d4ce9769890be0c6c36ecb9a03d73645c6fb588fd85f8dbc13f0c32503e7`
- daytona / cleanup_verification: `ecb4ba3001c71212fefe3795b43018345595301d5f6b63446a0b98409fadc597`
- daytona / provider_compare: `8291db81299c8cd4892a99ddf795b199566506cde613d8109ddca01294209b06`
- daytona / step_candidate: `e0d2d4ce9769890be0c6c36ecb9a03d73645c6fb588fd85f8dbc13f0c32503e7`
- e2b / cleanup_verification: `ecb4ba3001c71212fefe3795b43018345595301d5f6b63446a0b98409fadc597`
- e2b / provider_compare: `8291db81299c8cd4892a99ddf795b199566506cde613d8109ddca01294209b06`
- e2b / step_candidate: `e0d2d4ce9769890be0c6c36ecb9a03d73645c6fb588fd85f8dbc13f0c32503e7`
- tzafon / cleanup_verification: `ecb4ba3001c71212fefe3795b43018345595301d5f6b63446a0b98409fadc597`
- tzafon / provider_compare: `8291db81299c8cd4892a99ddf795b199566506cde613d8109ddca01294209b06`
- tzafon / step_candidate: `e0d2d4ce9769890be0c6c36ecb9a03d73645c6fb588fd85f8dbc13f0c32503e7`

All arms completed with zero failures, zero harness retries, zero replacement samples, and clean resource cleanup. Provider SDK retry policy is shown per arm.
