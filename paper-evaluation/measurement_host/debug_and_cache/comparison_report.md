# Evaluation Report: scenario 2, comparison_session 10

Generated 2026-10-23T06:51:57.718110+00:00.

## 1. Setup

Version `a` is the baseline: every difference below is `b` minus `a`, following `run-session`'s argument order. Only repetitions that reached `ok` are included.

|  | Version | `app_version` | Scheduled | Usable | Retried | With notes |
|---|---|---|---|---|---|---|
| Baseline (`a`) | InvenTree 1.4.2 Session Flow DEBUG=False cache=True | 7 | 30 | 30 | 5 | 5 |
| Compared (`b`) | InvenTree 1.4.2 Session Flow DEBUG=True cache=False | 8 | 30 | 30 | 4 | 4 |

| Setting | Value |
|---|---|
| Host | `measurement_host` |
| METRION config | `/opt/metrion-runtime/config.yaml` |
| Measured host idle power | 16.689 W (`package_0`) |
| CPU governor | `performance` |
| Host hardware and OS | Intel(R) Core(TM) Ultra 9 285K, 24 cores, kernel 7.0.0-31-generic, Ubuntu 26.04.1 LTS (Resolute Raccoon), bare metal |
| METRION build | `metrion-0.1.0`, package `0a7b4649340749c0` |
| Random seed | `20260922` |
| Equivalence check | PASS |

Each repetition's energy covers its declared steps, from the first step's start to the last step's end, rounded outward to whole seconds; stack reset, readiness wait, browser launch and login are outside it. CPU energy is METRION's per-thread energy summed over the target stack's containers, including the idle share METRION attributes to them.

Execution order (randomized, read left to right):

```
run  1-10  a a b a a b a a a b
run 11-20  a b b a a a b a a b
run 21-30  a b b a a a b b a b
run 31-40  a a b b b b a a b a
run 41-50  a a b b b a b b b b
run 51-60  a b a b a a b b b b
```

![Run-order trend](fig3_run_order_trend.png)

## 2. Descriptive Statistics

| Metric | `a` InvenTree 1.4.2 Session Flow DEBUG=False cache=True | `b` InvenTree 1.4.2 Session Flow DEBUG=True cache=False |
|---|---|---|
| CPU energy n | 30 | 30 |
| CPU energy mean (J) | 416.03 | 467.34 |
| CPU energy SD (J) | 11.65 | 8.42 |
| CPU energy median (J) | 413.38 | 469.03 |
| CPU energy min (J) | 395.42 | 447.83 |
| CPU energy max (J) | 447.23 | 481.80 |
| Runtime mean ± SD (s) | 38.0 ± 1.0 | 37.9 ± 0.8 |

![Energy distribution](fig2_energy_distribution.png)

## 3. Comparison

|  | CPU energy |
|---|---|
| Difference `b` - `a` (J) | +51.31 |
| Welch 95% CI (J) | [+46.04, +56.57] |
| Relative difference | +12.33% |
| Fieller 95% CI | [+10.97, +13.71]% |
| Welch t (df) | 19.55 (52.8) |
| Welch p | < 0.001 |
| Mann-Whitney U p | < 0.001 |
| Run-order-adjusted difference (J) [95% CI] | +52.43 [+47.05, +57.82] |

Welch's t-test does not assume equal variances. The relative difference is `b` over `a` minus one, with a Fieller interval. Mann-Whitney U is a robustness check. The run-order adjustment is a least-squares fit of energy on version and execution slot.

![Energy comparison](fig1_energy_comparison.png)

## 4. Per-Container Energy

| Container | `a` mean (J) | `b` mean (J) | Difference (J) | Welch 95% CI (J) |
|---|---|---|---|---|
| `inventree-worker` | 148.94 | 179.96 | +31.02 | [+28.25, +33.78] |
| `inventree-db` | 147.57 | 166.85 | +19.28 | [+17.64, +20.91] |
| `inventree-server` | 109.60 | 112.80 | +3.20 | [+0.06, +6.34] |
| `inventree-proxy` | 6.41 | 6.39 | -0.01 | [-0.15, +0.13] |
| `inventree-cache` | 3.52 | 1.33 | -2.18 | [-2.22, -2.14] |

Mean CPU energy per repetition per container. The intervals are not adjusted for the number of containers.

![Container breakdown](fig4_container_breakdown.png)

![Per-container change](fig5_container_deltas.png)

## Supplementary Figures

Active and idle CPU energy per version; idle is METRION's time share of the measured idle power.

![Active and idle energy](fig6_energy_split.png)

Thread-level CPU energy inside the highest-energy containers.

![Thread breakdown](fig7_thread_breakdown.png)

Mean CPU energy measured during each declared step, in scenario order.

![Per-step breakdown](fig8_step_breakdown.png)

Change in energy measured during each step; the background worker queue shifts work between steps, so these are not per-step effects.

![Per-step change](fig9_step_deltas.png)

Mean energy per iteration of the repeated step block, with SD whiskers; work also shifts between iterations.

![Per-iteration energy](fig10_iteration_trend.png)
