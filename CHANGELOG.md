# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.13.0] - 2026-11-27

### Added

- Maintenance window. New inputs options: *Maintenance entity* (script,
  input boolean, binary sensor or switch; a script is on while it runs, such
  as the one that updates every ESPHome device) and *Margin after
  maintenance* (10 minutes). While the entity is on and for the margin
  after, TankWise keeps its plan and safeties but learns nothing (heater
  power, losses, capacity and usable factors, stratification, cut-offs,
  demand profile), does not replan for deviations or draws (no rescue
  recharge), does not count shortages, external orders, deviations or
  safety cuts, and does not enter degraded mode or put the plan at risk
  because the probe or the regulator are missing unless the plan is really
  at risk. Draws that overlap a window are kept but flagged and not learned.
- Automatic detection, even without an entity: a device of the inputs that
  becomes unavailable and returns within 15 minutes, or that restarts (its
  uptime sensor on the same device goes back, in seconds or as a boot
  time), opens the same window. A longer outage is a real one.
- Binary sensor *Maintenance in progress* (`source`, `until`, `devices`) and
  a `maintenance` attribute on the heating plan.
- **Automatic compensation cap.** The compensation (export × injection price)
  and the energy term (import × purchase price) of the billing period are
  added up hour by hour from the grid meters, and projected to the end of
  the period with the hourly rate of the last 14 days. When the projected
  compensation exceeds the projected energy term, that excess would be
  exported for nothing: the plan takes that much surplus for free and values
  the rest at the injection price. Before, only the switch did it, by hand
  and for all the surplus. The switch still forces the cap.
- Sensors **Surplus compensation this period**, **Energy term this period**
  and **Compensation cap margin** (projected; negative: over the cap), with
  the projection, the margin so far and the surplus over the cap.
- Advanced parameter **Billing period start day** (1: the calendar month).
- Advanced parameter **Energy term factor for the compensation cap** (1.0):
  multiplies the computed energy term, for bills that cap the compensation
  at the energy term without tolls and charges. Calibrated with a bill:
  energy term of the bill ÷ the sensor's energy term without the factor over
  the same period.
- The past hours of the period come from the recorder statistics (hourly
  sums of the energy sensors, means of the price sensors) at every start.

### Notes

- The energy term uses the purchase price sensor as it is: the PVPC includes
  the access tolls and charges of the energy term, which cannot be separated
  (hence the factor).

## [0.12.4] - 2026-11-24

### Fixed

- With the probe in the thermostat's sheath the model could fall far below
  what the probe allows (24/11: 16.0 °C with the probe at 27.5 °C, 8 L
  available): draws are counted at the temperature of the hot layer, which
  overcounts them as that layer runs out. The probe now bounds the model at
  every sample: in a stably stratified tank the water above the probe is at
  least at its temperature and the water below it at least at the mains
  temperature, so the mean is at least `p · T_mains + (1 − p) · T_probe` and
  the hot layer at least the probe. `p` is the new advanced parameter
  *Volume below the probe* (30 %). Not the probe itself: the cold water fills
  the bottom below the sheath first (24/11: the probe stayed at 68 °C for
  the first 30 L of the evening while the mean fell to 60 °C).

### Notes

- The cut-off of 24/11 at 13:49 is in the list: 69.15 °C, learned at
  13:52:58 when it was confirmed. The sample is the filtered probe at the
  confirmation (3 minutes after the cut), not the raw reading at the cut
  (69.4 °C).

## [0.12.3] - 2026-11-24

### Fixed

- Cut-off samples were lost in a history import: the list with more samples
  won, and an import of the last 10 days lacks the older ones (24/11: the
  69.4 °C cut-off of 13:49 was missing after installing 0.12.2). Samples now
  keep their time; the import is merged by union, without duplicates (same
  time and value; by value for samples saved before this version), keeping
  the 20 most recent. Samples discarded with the service are remembered and
  not brought back by an import.

### Added

- **Probe position** parameter (*Options → Parameters*): top of the tank (the
  default, as before) or the thermostat's sheath, low in the tank next to the
  element. In the sheath the probe falls when the cold front reaches it, long
  before the hot water at the top runs out (24/11: 32 °C with some 55 L at
  40 °C still available). With the sheath:
  - a hot water shortage is not taken from the probe but from the model (no
    useful energy left): on 24/11 nine shortages in a week were false, and
    raised the demand forecast;
  - the useful litres factor (`k_use`) is not learned from the probe crossing
    the comfort temperature;
  - the estimated outlet temperature is the hot layer (the last full charge
    less the losses), not the model plus the stratification offset;
  - with a hot water meter, draws leave at the hot layer's temperature (the
    probe's 59, 51, 40 °C undercounted the evening showers) and the probe
    does not correct the metered balance between full charges;
  - a partial heating puts its energy into the whole tank: the hot layer
    only rises once the mean passes it (the element heats the cold water at
    the bottom, which cannot rise through hotter water), and the useful
    litres follow the energy;
  - the useful litres factor is the *Default useful litres factor* parameter
    (0.85, advanced) while none is learned (with the probe at the top it
    stays 1.0).
- Service `tankwise.reset_usable_factor`: forgets the learned useful litres
  factor and returns the samples removed and the factor in use.

### Notes

- Set *Probe position* to *Thermostat sheath* if the probe is in the
  thermostat's sheath. The useful litres factor learned so far came from the
  probe crossing the comfort temperature: reset it with
  `tankwise.reset_usable_factor`.

## [0.12.2] - 2026-11-24

### Fixed

- A real thermostat cut-off was discarded. On 24/11 at 13:49:27 (probe
  69.4 °C) the diverter was modulating in Auto (77 → 73.8 → 72.7 %); with no
  consumption it raised its output to 100 % at 13:49:32 and went to 0 % at
  13:50:02 (the surplus taken elsewhere). Three rules discarded it: the small
  swings before the drop counted as the regulator stopping, the 0 % after the
  drop counted as a disturbance, and as the regulator's own late stop.
  - The mode, the output and the manual mode are checked only up to the drop.
    After it, only the regulator becoming unavailable or restarting (up to
    3 minutes) discards it: a mode or output change after the drop is a
    consequence of the cut-off.
  - Before the drop, only an output stopping (down to 10 % or by 30 points)
    or the manual mode switched off counts as the regulator stopping it, not
    a regulator modulating by a few points.
  - An output at rest reported within a minute of the drop is still the
    regulator's own stop arriving late (a cloud: on 24/11 at 13:41 the power
    fell to 0 and the 0 % arrived 2 s later), unless the regulator first asked
    for more power (output at 95 %, 10 points above its value at the drop, or
    the manual mode switched on), as it does when the thermostat cuts off. At
    full output or in manual mode at the drop it cannot rise: held there for
    5 s after the drop counts as asking for more.
- The cut-off sample is the probe's temperature and a confirmed cut-off sets
  the model full at it, also in a stratified tank (now covered by a test).

### Notes

- The history is imported again after upgrading (models version 6), so the
  cut-offs of the last 10 days are taken again with the new rule: the one of
  24/11 counts without waiting for the next one.

## [0.12.1] - 2026-11-23

### Fixed

- With the stratification learned, the available litres, the temperature an
  appointment needs and the plan treated the tank as mixed: only the heat
  above the comfort temperature of the mixed equivalent was useful. On 23/11
  at 20:45 (about 3.6 kWh above the mains water, 44.3 °C mixed, 56.9 °C at
  the probe) TankWise showed 21 L available, asked 70.3 °C for the evening
  appointment and planned a grid heating at 21:00. Once the stratification
  is learned (at least 3 samples, the probe falling at most 80 % of a mixed
  tank) the useful energy is `k_use` times the energy above the mains water:
  about 104 L available, 45.8 °C for the evening appointment, and no grid
  heating before midnight. The appointments, the boost by litres, the plan
  and its risk use the same useful floor. Without learned stratification
  nothing changes.

### Added

- `sensor.tankwise_outlet_temperature` (estimated outlet temperature): the
  hot layer at the outlet, consistent with the probe, next to the modelled
  (mixed) temperature. `stratified` and `useful_floor_temperature` attributes
  in the available litres sensor.

## [0.12.0] - 2026-11-23

### Added

- Optional hot water meter at the outlet of the tank (**Options → Inputs**):
  cumulative volume in L or m³ (`total_increasing`, `total` or no state
  class, as pulse counters publish it) and, optionally, its flow rate. With
  it the draws are measured instead of estimated from the probe:
  - a draw starts with the first litre and ends after 3 minutes without a
    new one (single pulses are grouped; it goes on while the flow is above
    zero);
  - its energy is `litres · 1.163 Wh/(L·K) · (T_out − T_mains)`, with the
    tank temperature when it started: in a stratified tank the water leaves
    hot nearly the whole draw, so the drop of the probe does not lower it.
    On 23/11 34 L left at about 67 °C: 2.1 kWh, where the probe gave 0.98;
  - the litres are counted while the probe is unavailable, so a shower
    during a gap of the probe is no longer lost;
  - the model learns the stratification from the metered energy and the
    probe drop (draws worth at least 4 °C in a mixed tank, without heating),
    by temperature, instead of assuming a mixed tank; the probe corrects the
    model only beyond 1 °C (resolution and error of a MAX6675 thermocouple);
  - measured draws are never scaled by the energy reconciliation, so the
    useful litres factor (`k_use`) is learned from exact energies.
- Litres in the demand profile: each day measured by the meter keeps the
  litres of each 15-minute slot, and the demand of today gains the litres of
  a usual day.
- Suggested litres of the appointments from the measured draws: with a meter
  the suggestion (still in litres at the shower temperature) uses only the
  metered days, each draw worth
  `litres_tank · (T_tank − T_mains) / (T_shower − T_mains)` with the tank
  temperature when it started (34 L at 67.5 °C are 69 L at 40 °C with the
  mains at 13.5 °C). The appointment requirement uses it too. New
  attributes `shower_liters_p50`, `shower_liters_p90`, `hot_liters_p50`,
  `hot_liters_p90` (litres that left the tank), `hot_days` and `source`
  (`meter` or `probe`). Without a meter nothing changes.
- Adding a hot water meter imports the history again, so the draws of the
  imported days are measured with its history.
- `liters` in `sensor.tankwise_last_draw`, in the draws of the diagnostics
  and the stratification samples in the model status and diagnostics.

### Fixed

- `sensor.tankwise_demand_today` ignored what was drawn today: it showed the
  P90 of a whole usual day from the profile. It is now what was drawn today
  plus the P90 of the rest of the day, with `observed` (kWh),
  `observed_liters` (with a meter) and the whole day of the profile
  (`profile_p50`, `profile_p90`) as attributes.
- The estimated draw across a gap was marked `while_heating` when the energy
  meter moved by a few mWh (on 23/11, 1 mWh from a 1.9 W blip of the Shelly
  at 18:40). A heating now needs at least 20 Wh.

### Notes

- Without a hot water meter the tank model behaves exactly as in 0.11.1
  (checked against a recorded day of 0.11.1).
- The history is imported again once after upgrading (models version 5).

## [0.11.1] - 2026-11-23

### Fixed

- A draw during a gap of the probe readings was followed but not recorded:
  on 23/11 the probe was unavailable for 78 minutes while the diverter was
  dismounted, and a shower (68 → 59 °C, about 1 kWh) never reached the demand
  profile. When the readings come back (gaps up to 12 hours), a drop beyond
  what the losses and the metered heat explain (1 °C plus 0.25 °C per hour of
  gap and 10 % of the heat, for the drift of the prediction) is recorded as an
  estimated draw spread over the gap: `estimated` in the draw, in
  `sensor.tankwise_last_draw` and in the diagnostics. Unknown heat can only
  hide part of a draw, so the energy is a lower bound.

### Notes

- Only a gap of the probe or the heater power makes the state uncertain
  (`data_gap`). The diverter being unavailable with the probe and the meter
  working is not one: the model keeps its calibration and detects the draws
  as usual (now covered by a test).

## [0.11.0] - 2026-11-23

### Fixed

- A power cut of the diverter was learned as a thermostat cut-off: the
  element stops drawing power, but the regulator entities keep their last
  state (100 %, manual off) for about two minutes before they are reported
  unavailable. On 23/11 a cut at 11:47:56 became a 60.9 °C sample. A power
  drop is no longer a cut-off when, from one minute before it to three
  minutes after it, the regulator was unavailable or restarted (lower uptime
  or a new restart reason), or when, from one minute before it to two minutes
  after it, it was in a mode that does not heat or asked for 0 %. A mode or
  setpoint change is reported at once, and later than that it is usually an
  automation reacting to a real cut-off. The history import applies the same
  rule.
- Once a cut-off is known, a new sample must be within 4 °C of it (it was
  12 °C below the configured cut-off). Until the first sample the old rule
  applies.

### Added

- Optional inputs for the regulator health: mode entity and the modes that
  heat (Auto and Manual by default), uptime sensor and restart reason sensor.
  The regulator output switch, if configured, also counts when unavailable.
- Services `tankwise.discard_last_cutoff_sample` and
  `tankwise.discard_cutoff_sample` (by `value` or `index`) to forget a wrong
  cut-off sample; the cut-off, the model status and the plan are computed
  again. Both return the sample removed and the samples left.

## [0.10.2] - 2026-11-22

### Fixed

- The demand forecast could give a slot a P90 below its P50 (e.g. 19:30:
  P50 1.22 kWh, P90 0), and a P90 evening below the P50 one: slots were
  increments of the quantile of the cumulative curve, which are not
  quantiles (wrong when the heavy days shower earlier). Each slot is now the
  weighted mean shape of the days scaled to the P50/P90 of the daily total,
  so the P90 is never below the P50 in any slot or window and peaks at the
  same time. Daily totals, appointment blocks and the demand left today
  (quantiles of window totals) do not change.

### Added

- `sensor.tankwise_model_status` attributes: `reason` (`never_calibrated`,
  `data_gap`, `few_loss_windows` or `calibrated`), `missing` (a heating to
  the cut-off, loss windows), `uncertain_since`, the last calibration and the
  samples of each learned parameter (cut-off, heater power, heat losses,
  capacity and usable factors, profile days) with the samples needed.

## [0.10.1] - 2026-11-20

### Fixed

- The history import after an upgrade did not start: while Home Assistant
  was starting it failed (`async_at_started` called on the wrong object).
  It also gave up after 30 minutes waiting for the probe and the power; it
  now keeps waiting until they report values.
- Shortage uplifts compounded: several shortages around the same hour
  multiplied the forecast by 1.2 each (×1.5 and more for a usual evening),
  so an appointment block could ask for 83 °C. The uplift is now at most
  ×1.2 per slot, and `required_temperature` / `suggested_liters` of an
  appointment use it too, so they agree with the plan.
- A stray small draw (e.g. a 5 L tap at 23:30 at P90) plus the reserve made
  the plan heat at an expensive hour with the tank below comfort. The demand
  of a slot and the reserve are now required only when the demand expected
  until a cheaper slot (or one with surplus) reaches the reserve.
- The plan bought energy at the end of the horizon to leave it in the tank:
  the energy left at the end was valued above the prices of the last day.
  It is now valued at most at the cheapest price of the last 24 hours.
- The day after tomorrow had no sun at all (forecasts cover two days), so the
  plan heated from the grid at the end of the horizon on sunny days. Days
  beyond the forecast now repeat its last day until the real one arrives.

### Added

- `risks` attribute of the plan sensor (also in `tankwise.get_plan` and the
  diagnostics): each run of slots where the plan cannot keep the comfort
  limits, with its start and end, kind (`appointment` or `demand`), the
  appointment and the litres missing. A plan at risk with all its blocks
  fine is explained by a `demand` risk.

## [0.10.0] - 2026-11-20

Seventh phase: migration from automations and full documentation.

### Added

- Repair issue "Other automations control the water heater": in the modes
  that act, lists the enabled automations that switch the regulator manual
  mode, its output or the water heater switch, which would fight the
  controller. Checked every five minutes.
- Migration guide (`docs/migration.md`) and architecture description
  (`docs/architecture.md`), linked from the README.

### Changed

- The option of the daily solar forecast sensor, unused since 0.9, is
  removed from existing entries (config entry version 2.3).
- `automation` added to the integration's after dependencies.

## [0.9.0] - 2026-11-20

Sixth phase: solar forecast, selling the surplus, coordination with other
loads and the anti-legionella cycle.

### Added

- Solar production forecast from one or more config entries of
  Forecast.Solar, Solcast or Open-Meteo Solar Forecast (their hourly Energy
  dashboard forecast), summed. The plan counts on the expected surplus: the
  forecast minus the house consumption of each hour, learned from the grid
  export and the energy the water heater took from the surplus (which also
  corrects the bias of the forecast).
- Selling the surplus: with the new regulator output switch input (FreeDS
  "PWM enabled"), the output is switched off when the plan lets the surplus
  go to the grid (the injection pays at least 0.03 €/kWh more than heating
  later), and on again afterwards and before any heating.
- Requested surplus power sensor with a `high`/`low` priority and the
  `tankwise_power_request` event, for surplus managers such as SolarSplit.
- Anti-legionella cycle (switch, on by default): 60 °C or more for an hour at
  least once a week. Cycles are measured by the probe; when the deadline is
  two days away the plan adds a 61.5 °C target after the cheapest hour to
  heat (surplus first). Last anti-legionella cycle sensor with the deadline
  and the planned time.
- Diagnostics: learned house consumption per hour, forecast, power request
  and anti-legionella state.
- Simulation bench with a surplus forecast (naive and perfect) to measure
  what the forecast is worth.

### Fixed

- Real cost: heating with the regulator in automatic mode counts as surplus
  even when the export is close to zero (the diverter keeps it there while
  it heats); before, it was counted as grid energy at the purchase price.
- Heat losses inflated by slow draws (a live model reached 1.86 W/K against
  0.94 W/K): a rest window ended by a draw drops its last hour (the slow
  draw detector looks an hour back, so the beginning of the draw was taken
  as losses), and once UA is learned a window implying less than half or
  more than 1.8 times the current value is rejected.
- History import merge: an estimate of the running model that is not
  coherent with the history (UA more than 30 % apart, heater power more
  than 10 %) gives way to the history, whatever its evidence. Lists of
  measurements (cut-offs, capacity and usable factors) still keep the source
  with more of them. The history is imported again on upgrade (bootstrap
  version 4), also from 0.6.
- The two-hour limit of grid heating runs now counts the heating already
  done before each replan (found by the simulation bench: a run of 151
  minutes with the solar forecast; the controller safety would have cut it
  at two hours).

### Changed

- The daily solar forecast sensor input is replaced by the forecast entries
  (a daily total cannot place the surplus in the day). Choose the entries in
  the options after upgrading.
- The plan lets the surplus go to the grid only when it pays at least
  0.03 €/kWh more than heating later.

## [0.8.0] - 2026-11-20

Fifth phase: boost (hot water now) and a blueprint for push buttons.

### Added

- Boost: the regulator goes to manual mode at once, whatever the plan and
  the price, in every mode except off (also shadow and holiday). It ends at
  the boost target (number, 60 °C), the thermostat cut-off, the optional
  maximum duration (number, 90 minutes, 0 for no limit), a safety, or when
  cancelled; the safeties are never skipped. If the water is already above
  the target nothing happens (logbook entry). A boost in progress survives a
  restart, and the plan is computed again when it ends.
- Boost button (pressed again while running, cancels), cancel boost button,
  boost active binary sensor (start, target, expected and maximum end,
  energy, cost), `tankwise.boost` (optional `temperature` or `liters`, and
  `duration`) and `tankwise.cancel_boost` services, and the
  `tankwise_boost_started` / `tankwise_boost_finished` events (reason
  `target`, `thermostat`, `duration`, `safety` or `cancelled`).
- Option "Count a boost as a hot water shortage" (off by default): raises
  the forecast around the time of each boost.
- Blueprint `blueprints/automation/tankwise/boost_from_button.yaml`: start,
  cancel or toggle a boost from any trigger (ZHA, Zigbee2MQTT, Z-Wave or
  Shelly buttons, remotes, events), with an optional temperature and
  duration.

### Upgrading

- From 0.6.0 directly: the history is imported again (bootstrap version 3)
  and merged with what is stored, as when upgrading through 0.7.0.

## [0.7.0] - 2026-11-20

Fourth phase: TankWise controls the regulator in the auto, eco, comfort and
holiday modes. Shadow stays the default mode.

### Added

- Controller: the regulator goes to manual mode during the planned minutes
  of each slot (a whole slot or its first half) and back to automatic
  afterwards. Orders are sent when the desired state changes and repeated if
  they do not take effect; external orders are respected until the next
  change. Every order is logged in the logbook with its reason.
- Safeties, whoever switched the manual mode on: safety maximum temperature
  for one minute (number entity, 72 °C), thermostat cut-off (no power for
  three minutes), more than two hours in manual mode (15-minute pause), probe
  unavailable or not updated for ten minutes. After a cut-off or the safety
  maximum there is no grid heating until the tank cools 3 °C.
- Regulator lost: if it stays unavailable beyond a margin (advanced option,
  2 minutes) while TankWise has it in manual mode, or while the element heats
  outside the plan, the heater switch is opened as a last resort. When the
  regulator returns it is set to automatic and the heater switch is closed
  again (also after a restart). Repair issue after three cuts in a week.
- Heater switch off (not by TankWise): no action and a repair issue.
- Degraded mode (binary sensor) when there is no plan or no probe. Without a
  plan, before each active appointment the tank is heated to the temperature
  the appointment needs (limited to the learned cut-off), starting as early
  as the learned power requires; only without active appointments, fixed
  windows (50 °C 04:00-06:15, thermostat cut-off 15:00-16:00). Not entered
  in the first five minutes after a start, while prices load.
- Plan at risk also when the regulator is unavailable and an appointment
  depends on a planned heating, or the probe is unavailable before an
  appointment (`reasons` attribute), and the `tankwise_plan_at_risk` event
  when it turns at risk for a new reason (announced once, also across
  restarts).
- `tankwise.get_plan` service: the whole plan, slot by slot.
- `tankwise.reimport_history` service and "Import history again" button
  (diagnostic): import the recorder history again, merged with what was
  learned.

### Fixed

- Upgrading to 0.6.0 could erase everything learned. The history import
  (run again after an upgrade) replaced the running model with the imported
  one, and right after Home Assistant starts the input sensors may not exist
  yet: their units were unknown, every sample was read as missing and an
  import of thousands of empty samples was marked as done. Now:
  - the import is merged with what was learned: each parameter (heat
    losses, heater power, capacity and usable factors, thermostat cut-offs)
    keeps the source with more evidence, and draws and observed days are
    joined;
  - it waits until the entry is complete (no missing required inputs) and
    the probe and the power report values (up to 30 minutes after a start);
  - an import that learns nothing is not adopted (`empty` status) and runs
    again at the next start;
  - stored sections that cannot be read are kept aside instead of being
    overwritten.
- The history is imported again automatically when upgrading to this
  version, merged with what is stored.

### Changed

- The heating plan sensor publishes a compact summary (grid heatings and
  appointment blocks); the slot by slot plan moved to the diagnostics and
  the `tankwise.get_plan` service. Its attributes exceeded the 16 KiB the
  recorder stores, and the attributes meant to be excluded from the recorder
  (forecast and plan slots) were not, because they were set per entity
  instead of per class.
- The plan never aims above the safety maximum temperature.
- Logbook messages live in one module shared by the planner and the
  controller.

### Development

- Tests: the SQLAlchemy engine logger is set to WARNING (the test plugin
  sets it to INFO and printed every recorder query); regression test for the
  PWM swinging 0-100 % near the thermostat cut-off (a single cut-off).

## [0.6.0] - 2026-11-20

Third phase: TankWise computes and publishes the heating plan (shadow mode).
It still does not act on any device.

### Added

- Prices per 15-minute slot from the purchase price sensor (hourly prices of
  today and tomorrow as attributes; today's prices repeated until tomorrow's
  are published) and the value of the solar surplus: injection price, a fixed
  value (number entity) without injection sensor, or zero when the
  compensation cap of the month is reached (switch entity).
- Heating plan by dynamic programming over 36 hours: grid energy per slot and
  whether to take the solar surplus or let it go to the grid. Appointments
  are hard constraints on useful litres at the start of each block (larger of
  the minimum and the P90 of the whole block); outside blocks each slot with
  demand must be covered plus a reserve (number entity, 10 litres). Grid
  heating runs last two hours at most. The energy left at the end is valued
  at the median night price.
- Rescue recharge (option, on by default; off is strict mode): a block that
  needs more than a full tank is reached full, and inside the block only the
  energy missing to cover the P90 left in it is heated, never after the
  block. Rescues are logged in the logbook, marked in the plan and counted
  per month in kWh and €. The plan at risk turns on only when not even the
  rescue covers an appointment.
- Mode select: auto, eco (P50), comfort (P90 + 20 %), holiday (no grid),
  shadow (default) and off. Until the controller exists every mode only
  publishes the plan.
- Sensors: heating plan (cost of the horizon, slots as an attribute), next
  heating, predicted temperature per appointment, real and planned cost today
  and this month, estimated savings this month, rescue energy and cost this
  month. Binary sensor: planned grid heating now.
- Replan button and `tankwise.replan` service; replanning at every slot and
  when prices, draws, mode, occupancy, appointments or settings change, or
  when the tank drifts more than 3 °C from the plan.
- In shadow mode, external orders on the regulator manual mode that
  contradict the plan are logged and counted.
- Simulation bench on exported history (walk-forward, opt-in with
  `TANKWISE_BENCH=1`). On the reference installation (13-20 November) the
  plan costs 0.45 €/day against 0.78 €/day really spent, with no hot water
  shortage against 9 real ones.

### Objective (not reached yet)

- Average cost of at most 0.30 €/day on the reference installation. The plan
  reaches 0.45 €/day in the simulation bench; the next step is the solar
  forecast (phase 6), so the plan can count on tomorrow's surplus instead of
  heating from the grid at night.

## [0.5.0] - 2026-11-20

### Changed

- "Hot water available" now shows the useful litres: water at the shower
  temperature that can be drawn before the outlet falls below the comfort
  temperature, `k_use · V · (T − T_comfort) / (T_shower − T_mains)`. The
  previous value assumed a perfectly stratified tank and overestimated the
  water 3 to 7 times (190 litres at 63.7 °C on the reference installation,
  where the water ran lukewarm after about 100 litres); it is kept as the
  `theoretical_liters` attribute.

### Added

- Usable factor `k_use`, learned from real shortages (litres drawn since the
  last heating until the water fell below the comfort temperature, over the
  useful litres predicted then); 1.0 at first, between 0.5 and 1.5.
- Per appointment, `sensor.tankwise_<appointment>_required_temperature`: the
  tank temperature needed when the block starts to cover the larger of the
  minimum litres and the P90 of the litres used during the block.
- `binary_sensor.tankwise_plan_at_risk`: on when an active appointment needs
  more than the learned thermostat cut-off, i.e. even a full tank does not
  cover its block (appointments are suspended with nobody home).

## [0.4.1] - 2026-11-20

### Fixed

- Installations upgraded from 0.3 kept the history imported by 0.3, so the
  usage profile started empty and the old draws were not reconciled. The
  history is now imported again when the last import was made with older
  models.

## [0.4.0] - 2026-11-20

Second phase: TankWise learns the household's hot water usage and forecasts
the demand. It still does not act on any device.

### Added

- Usage profile: each complete day (at least 20 hours observed, home
  occupied) is kept as the energy drawn in 96 slots of 15 minutes. Days are
  grouped as workdays and weekends/holidays (or per weekday, or all alike);
  the last 16 days of each type are used, recent ones weighing more.
- Forecast of the next 36 hours with P50 and P90 per slot, computed on the
  cumulative curve of each day so that a strong day is not inflated by
  showers that move from one slot to another.
- A draw that left the water below the comfort temperature raises the
  forecast of its time of day by 20 % for two weeks.
- Energy reconciliation: with the probe at the top of the tank, a draw is
  seen late and too small while the cold water stays at the bottom (morning
  showers) and too large when the cold front reaches the probe (evening
  showers). After each heating the probe sees the mixed tank, so the energy
  balance between two settled heatings gives the energy really drawn, and
  the draws detected in between are scaled to match (×0.5 to ×5). On the
  reference installation the morning showers went from 0.4-1.1 kWh to their
  real 0.6-1.0 kWh and the evening ones were lowered by 10-30 %.
- Slow draws: a drop of more than 1 °C beyond the standing losses over an
  hour at rest, confirmed by the robust slope of the hour, is recorded as a
  draw (e.g. a shower with a hot, full tank where the cold water barely
  reaches the probe). This also keeps such drops out of the loss learning:
  the learned coefficient went from 1.39 to 0.94 W/K on real data.
- Days of the usage profile are closed at 09:00 the next day, once the night
  heating has reconciled the evening draws.
- Optional Workday sensor: holidays are learned apart from workdays, and the
  coming days are checked with the Workday integration.
- Hot water appointments (up to six): start time, block duration (1 hour by
  default), days, minimum litres at the shower temperature and active or
  not. The minimum litres must be available when the block starts and cover
  the whole block without reheating. Each appointment has time, litres and
  active entities synchronised with the options, and a suggested litres
  sensor (P90 of the water used during the block in the last four weeks).
- Sensors: hot water demand today and hot water demand forecast (36 hours,
  slots as an attribute that is not recorded).
- `tankwise.reset_learning` service (heat losses, usage profile or
  everything).
- The history import also rebuilds the usage profile.

### Changed

- The options are a menu: installation parameters, inputs and hot water
  appointments. Heating power, ambient temperature, thermostat cut-off and
  the usage profile settings are folded under "Advanced options".
- The heat loss coefficient, learned heating power, thermostat cut-off
  temperature and effective capacity factor sensors are disabled by default
  for new installations.
- Entity IDs no longer depend on the device name or area: they are always
  `<platform>.tankwise_<key>` (e.g. `sensor.tankwise_stored_energy`).
  Entities created by 0.3.0 with the area in their ID (e.g.
  `sensor.coladuria_tankwise_stored_energy`) are renamed in the entity
  registry and keep their history. Entity IDs changed by hand are kept.
- The 0.2 entities `sensor.tankwise_useful_energy`, `sensor.tankwise_charge`
  and `sensor.tankwise_model_status` are kept: they are the same entities as
  before (same unique IDs and history), not duplicates. Since 0.3.0 useful
  energy and charge are measured above the minimum comfort temperature
  (38 °C) instead of the 0.2 "minimum useful temperature" (32 °C).

### Fixed

- Heating stopped by an automation (e.g. a boost to 45 °C that switches the
  diverter manual mode off) was learned as a thermostat cut-off, which left
  the learned cut-off temperature around 46 °C:
  - the manual mode switched off, or the PWM output falling to rest, up to
    60 seconds after the power falls now means the regulator stopped the
    heating (its state reaches Home Assistant a few seconds after the power);
  - when the regulator is configured but unavailable, a stop of the heating
    is no longer decided with the temperature backup criterion;
  - a cut-off more than 12 °C below the configured cut-off temperature is
    not accepted, and such cut-offs learned before are forgotten;
  - entries that still had the 0.2 defaults (47 °C cut-off and 1450 W) get
    the current ones (68 °C and 1500 W).
- The effective capacity factor is limited to 1.0 (it reached 1.057).
- A draw counts as a hot water shortage only if the probe fell at least
  1.5 °C below the comfort temperature (water kept being drawn below it), not
  when the end of a shower just touches it. On real data this kept the 8
  real shortages of a week and dropped the 2 that only touched 38 °C.

## [0.3.0] - 2026-11-20

First phase of the full control of the water heater: TankWise learns the
tank and the draws with the inputs it will use to control it. It still does
not act on any device.

### Added

- New required inputs: water heater power switch, regulator manual mode
  switch (e.g. FreeDS manual mode) and electricity purchase price.
- New optional inputs: regulator master entity, home occupied, grid
  injection price, grid import and export energy, current export power and
  ambient temperature around the tank.
- History import: on the first start the last 10 days of the recorder are
  replayed through the tank model, so losses, cut-offs and draws are known
  from day one.
- Sensors: stored energy, hot water available (litres at the shower
  temperature), modelled temperature, last draw (with energy, minimum
  temperature and shortage), hot water shortages in the last 7 days, and the
  diagnostic sensors heat loss coefficient (UA), learned heating power,
  thermostat cut-off temperature, effective capacity factor and mains water
  temperature.
- Number entities: shower temperature (40 °C), minimum comfort temperature
  (38 °C) and mains water temperature (0 = automatic).
- Repair issue when required inputs are missing (e.g. after upgrading).
- `tools/export_history.py` to export the history of the inputs through the
  Home Assistant REST API for the simulation bench.

### Changed

- Standing losses are modelled as `UA · (T − T_ambient)` and UA is learned by
  recursive least squares over rest windows of at least two hours.
- The effective capacity factor (share of the heating energy seen by the
  upper probe) is learned from heatings without draws.
- Draws are detected from the residual between the observed and the predicted
  probe change; each draw records its minimum temperature and whether it left
  the water below the minimum comfort temperature.
- The thermostat cut-off is detected with the manual mode switch as well as
  the regulator output, and the last 20 cut-offs are kept.
- The water heater energy sensor is now optional: without it the power is
  integrated.
- The mains water temperature of the region is interpolated day by day.
- Defaults follow the reference installation: 1500 W and 68 °C cut-off.
- Useful energy and charge are measured above the minimum comfort
  temperature.

### Removed

- The "showers available" sensor and the energy per shower, minimum useful
  temperature and comfort level options (replaced by the litres available and
  the comfort settings). Existing entries are migrated automatically.
- The bottom temperature probe input.

## [0.2.3] - 2026-11-17

### Added

- The TankWise device now shows its manufacturer, model, software version
  (taken from the integration manifest) and a link to the repository.
- The "Showers available" sensor has the attribute `energy_per_shower_kwh`,
  the energy per shower used to compute it.

## [0.2.2] - 2026-11-16

### Fixed

- A thermostat cut-off was missed when the regulator was switched off shortly
  afterwards (e.g. an automation turning off the diverter manual mode three
  minutes after the power drops): with 60-second samples only about two
  minutes of regulator demand were seen. The regulator value is now captured
  at the instant the power falls, from the state changes of the power and
  regulator sensors, and a full tank is confirmed if the power stays below
  the threshold for 3 minutes, even if the regulator goes to 0 afterwards.
  A power drop caused by the regulator being lowered first is still not a
  full tank.

## [0.2.1] - 2026-11-15

### Fixed

- Entity IDs are now generated from fixed English object IDs
  (`useful_energy`, `charge`, `showers_available`, `model_status`) whatever
  the Home Assistant language, so they no longer depend on the language
  (e.g. `sensor.<name>_useful_energy` instead of a Spanish entity ID).
  Visible names stay translated. Existing entities keep their current entity
  IDs.

## [0.2.0] - 2026-11-15

First public release (0.1.0 was an internal milestone and was not published).
TankWise models the tank and publishes its state of charge; usage learning
and the heating plan will follow.

### Added

- Config flow to select the tank temperature, heating element power and
  heating element energy sensors, with unit and state class validation.
- Reconfigure step to change the required sensors without removing the
  integration.
- Optional inputs: solar forecast, solar surplus, electricity price, presence
  switch, bottom temperature probe and regulator output (%).
- Options flow with the tank parameters: volume, rated power, mains water
  region and optional temperature override, minimum useful temperature
  (measured at the probe, 32 °C by default), cut-off power threshold,
  thermostat cut-off temperature, energy per shower and comfort level.
- Monthly mains water temperature tables for the Balearic Islands (default),
  generic mainland Spain and the Canary Islands.
- Tank thermal model (pure Python): filtered probe, energy balance with the
  heating element energy meter, draw detection (also while heating),
  recalibration to full on thermostat cut-off (regulator output as main
  criterion, cut-off temperature as backup), learned cut-off temperature and
  slowly learned standing losses.
- Sensors: useful energy, charge, showers available and model status.
- Model state persisted across restarts; outages longer than 30 minutes mark
  it as uncertain until the next full tank.
- Safe mode: if a required sensor is missing the model freezes and the
  sensors become unavailable.
- Diagnostics with the loss coefficient, the learned cut-off temperature and
  the recent draws.
- English and Spanish translations.
- Brand icons for Home Assistant 2026.3 and later.

### Notes

- Requires Home Assistant 2026.3 or later.
- The solar forecast selection is stored but not used yet. Reading the hourly
  forecast curve (Forecast.Solar through the Energy dashboard solar forecast
  platform, Solcast through the sensor attributes) will arrive with the
  planner.
- The output contract entities (energy needed, deadline, grid allowed,
  requested power and urgency) arrive in a later release.

[Unreleased]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.12.4...HEAD
[0.12.4]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.12.3...v0.12.4
[0.12.3]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.12.2...v0.12.3
[0.12.2]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.12.1...v0.12.2
[0.12.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jdgarcia99/ha-tankwise/releases/tag/v0.2.0
