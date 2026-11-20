# Migrating from automations to TankWise

Many installations already heat their water heater with automations: a
regulator (e.g. FreeDS) in manual mode at night, a top-up before the evening,
safety stops at a temperature, a push button that forces a heating. TankWise
replaces all of them. This guide moves from one to the other without a day
without hot water.

## 1. Install in shadow mode

Install TankWise and leave your automations as they are. TankWise starts in
**shadow** mode: it learns the tank and the household's usage, imports the
last days of history from the recorder and publishes its plan, but it does
not switch anything.

Complete the inputs in the options, in particular:

- the regulator manual mode and the water heater switch (required);
- the energy meter of the water heater and the grid export (recommended);
- the solar production forecast entries and the regulator output switch, if
  you have solar panels;
- the hot water appointments of the household (times the water must be ready,
  with a minimum of litres).

## 2. Compare for one or two weeks

- **Planned cost** vs **Water heater cost**: what the plan would spend against
  what your automations really spend.
- **Hot water shortages (7 days)** and the **Plan at risk** binary sensor: the
  plan should not foresee shortages that do not happen, nor miss real ones.
- The logbook lists every external order on the regulator manual mode that
  contradicts the plan: these are the automations that will conflict.
- Check the learned values in the diagnostics (heat losses, heater power,
  thermostat cut-off) and that the history import is `done`.

## 3. Switch to auto and disable the automations

Set the mode to **Auto** (or Eco or Comfort). TankWise then creates a repair
issue, **Other automations control the water heater**, listing every enabled
automation that switches the regulator manual mode, the regulator output or
the water heater switch. Disable them all: two controllers fighting over the
same switch give neither comfort nor savings.

Automations that only read the tank (notifications, dashboards) can stay.

## 4. Push buttons and remotes

Reassign them to the boost with the blueprint
`blueprints/automation/tankwise/boost_from_button.yaml`: choose the trigger,
the action (toggle, start only or cancel only) and optionally a temperature
and a duration. Delete the old button automations.

## 5. Clean up

Once everything has worked for a while:

- delete the disabled automations;
- delete the helpers they used (target temperatures, thresholds, enable
  flags): TankWise keeps its own settings as number, switch and select
  entities;
- keep the regulator in automatic mode by default: TankWise switches it to
  manual only when the plan, the degraded rule or a boost asks for it.

## Going back

Set the mode to **shadow** or **off**: TankWise gives the regulator back in
automatic mode (and switches its output on again if it had switched it off)
and stops acting. Enable your old automations again.
