<p align="center">
  <img src="images/banner.png" alt="TankWise" width="100%">
</p>

# TankWise

[![Validate](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml)
[![Tests](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml)

*[Read in English](README.md)*

TankWise es una integración de Home Assistant que toma el control completo de
un termo eléctrico de acumulación gobernado por un regulador de potencia (por
ejemplo un FreeDS con PWM). Decide **cuándo y cuánto calentar** para que
siempre haya agua caliente cuando la casa la necesita, al mínimo coste (precio
PVPC de compra más el valor del excedente solar).

## Estado del proyecto

> **0.10.0: las siete fases hechas.** TankWise aprende el depósito y el uso
> de la casa, calcula el plan de calentamiento y, en los modos Auto, Eco,
> Confort y Vacaciones, **controla el regulador** con sus seguridades. Empieza
> en modo Sombra (publica el plan sin actuar): sigue la
> [guía de migración](docs/migration.md) para dejar tus automatizaciones.

| Fase | Contenido | Estado |
|------|-----------|--------|
| 1 | Modelo térmico, detección de extracciones, importación del histórico, sensores (sin actuar) | ✅ Hecha |
| 2 | Perfil de demanda y previsión, citas de agua caliente | ✅ Hecha |
| 3 | Precios, valor del excedente y optimizador; modo Sombra con el plan | ✅ Hecha |
| 4 | Controlador, seguridades y modo degradado; modo Auto | ✅ Hecha |
| 5 | Boost con blueprint para pulsadores externos | ✅ Hecha |
| 6 | Previsión solar, coordinación con SolarSplit y antilegionela | ✅ Hecha |
| 7 | Migración desde las automatizaciones y documentación completa | ✅ Hecha |

## Instalación

1. En HACS, abre el menú y elige **Repositorios personalizados**.
2. Añade `https://github.com/jdgarcia99/ha-tankwise` con la categoría **Integración**.
3. Instala **TankWise** y reinicia Home Assistant.
4. Ve a **Ajustes → Dispositivos y servicios → Añadir integración → TankWise**.

## Configuración

Todo se configura desde la interfaz; no hay nada en `configuration.yaml` y
ningún `entity_id` está fijado en el código.

**Entradas obligatorias**

- Temperatura del depósito: sonda en la parte alta (°C o °F).
- Potencia del termo (W o kW).
- Interruptor de alimentación del termo (debe estar siempre encendido).
- Modo manual del regulador: interruptor que hace que caliente a plena
  potencia (p. ej. el modo manual de FreeDS).
- Precio de compra de la electricidad (p. ej. PVPC).

**Entradas recomendadas y opcionales**

- Energía del termo (kWh o Wh, `total_increasing` o `total`). Recomendada;
  sin ella se integra la potencia.
- Salida del regulador (%), p. ej. la salida PWM de FreeDS.
- Entidad que indica si el regulador es maestro del termo.
- Casa ocupada.
- Precio de inyección, energía importada y exportada, potencia exportada ahora.
- Previsión de producción solar: una o varias entradas de Forecast.Solar,
  Solcast u Open-Meteo Solar Forecast (sumadas).
- Interruptor de salida del regulador (p. ej. "PWM enabled" de FreeDS), para
  vender el excedente.
- Estado del regulador: su modo (con los modos que calientan, Auto y Manual
  por defecto), su tiempo encendido y el motivo de su último reinicio. Así una
  caída de potencia mientras el regulador no está disponible, se reinicia,
  está en otro modo o al 0 % no se toma como un corte del termostato.
- Temperatura ambiente donde está el termo.
- Sensor de días laborables (integración Workday): los festivos se aprenden
  aparte y las citas se pueden fijar para laborables o festivos.
- Caudalímetro de agua caliente a la salida del termo: volumen acumulado (L o
  m³; `total_increasing`, `total` o sin clase de estado, como lo publican los
  contadores de pulsos) y, opcionalmente, su caudal. Con él las extracciones
  se miden en litros en vez de estimarse con la sonda (ver más abajo).

**Opciones**

El menú de opciones tiene tres partes: parámetros de la instalación, entradas
y citas de agua caliente. Los parámetros solo muestran el volumen del depósito
y la región del agua de red; lo que TankWise aprende solo queda plegado en
**Opciones avanzadas**:

| Parámetro | Valor por defecto |
|-----------|-------------------|
| Volumen | 100 L |
| Región del agua de red | Islas Baleares |
| *Avanzada:* potencia de calentamiento (valor inicial, después se aprende) | 1500 W |
| *Avanzada:* temperatura ambiente sin sensor | 20 °C |
| *Avanzada:* temperatura de corte del termostato (valor inicial, después se aprende) | 68 °C |
| *Avanzada:* umbral de potencia de corte del termostato | 50 W |
| *Avanzada:* agrupar los días del perfil de uso | Laborables y fines de semana/festivos |
| *Avanzada:* días de cada tipo en el perfil de uso | 16 |

**Ajustes (entidades number)**

| Entidad | Valor por defecto |
|---------|-------------------|
| Temperatura de ducha | 40 °C |
| Temperatura mínima de confort | 38 °C |
| Temperatura del agua de red | 0 = automática (tabla mensual de la región, interpolada día a día) |

Las instalaciones que vienen de la 0.2 se migran solas y conservan lo
aprendido. Un aviso de reparación pide completar las nuevas entradas
obligatorias con **Reconfigurar**.

## Citas de agua caliente

Una cita dice "agua caliente lista a esta hora, con al menos esta cantidad
para el bloque que sigue": nombre, hora de inicio, duración del bloque (1 hora
por defecto, p. ej. 3 horas para las duchas de la tarde), días de la semana (o
laborables/festivos con el sensor Workday), litros mínimos a la temperatura de
ducha y si está activa. Los litros mínimos deben estar disponibles al empezar
el bloque y cubrirlo entero sin recalentar. Hasta seis, ninguna
por defecto. Se añaden en **Opciones → Citas de agua caliente**.

Cada cita tiene entidades para ajustarla desde un panel (los cambios se
guardan en las opciones): `time.<nombre>_<cita>_time`,
`number.<nombre>_<cita>_liters` y `switch.<nombre>_<cita>_enabled`. El sensor
`sensor.<nombre>_<cita>_suggested_liters` muestra los litros que realmente se
usan durante el bloque (P90 de las últimas cuatro semanas, aumentado como en
el plan tras faltas recientes, a la temperatura de ducha) para ayudar a elegir
el mínimo. Con caudalímetro la sugerencia sale de las extracciones medidas:
cada una equivale a `litros_termo · (T_termo − T_red) / (T_ducha − T_red)`
litros a la temperatura de ducha, con la temperatura del termo al empezar
(atributos `shower_liters_p50` y `shower_liters_p90`, y `source`);
`hot_liters_p50` y `hot_liters_p90` dan los litros que de verdad salieron del
termo. Y `sensor.<nombre>_<cita>_required_temperature` la
temperatura que necesita el depósito al empezar el bloque. Si supera el corte
aprendido del termostato, `binary_sensor.tankwise_plan_at_risk` se enciende:
ni el depósito lleno cubre el bloque. Desde la fase 3 el planificador trata
cada cita activa como una restricción obligatoria sobre los litros útiles.

## Entidades

Los `entity_id` se generan en inglés a partir del nombre de la instancia (por
ejemplo `sensor.<nombre>_available_liters`) sea cual sea el idioma; los
nombres visibles están traducidos.

| Entidad | Descripción |
|---------|-------------|
| Energía almacenada | kWh almacenados por encima de la temperatura del agua de red |
| Energía útil | kWh almacenados por encima de la temperatura mínima de confort |
| Carga | Energía útil en porcentaje de un depósito lleno |
| Agua caliente disponible | Litros útiles a la temperatura de ducha antes de que el agua baje de la temperatura de confort; en un depósito estratificado, `k_use` por la energía sobre la red (atributos: litros teóricos, `stratified`, `useful_floor_temperature`) |
| Plan en riesgo (binario) | Encendido cuando no se puede garantizar el agua caliente: una cita necesita más de lo que da el depósito lleno, la demanda habitual no se cubre, o el regulador o la sonda no están disponibles antes de una cita (atributo `reasons`) |
| Temperatura modelada | Temperatura equivalente (mezclada) del depósito según el modelo: la energía almacenada expresada como temperatura |
| Temperatura de salida estimada | Temperatura de la capa caliente a la salida, la que ve la sonda: la temperatura modelada más la estratificación esperada (igual a ella en un depósito mezclado) |
| Estado del modelo | `calibrated`, `learning` o `uncertain`; atributos `reason`, `missing` (lo que falta para calibrar) y las muestras (`samples`) de cada parámetro aprendido |
| Última extracción | Inicio de la última extracción, con su energía, sus litros (con caudalímetro), temperatura mínima y si hubo falta de agua |
| Faltas de agua caliente (7 días) | Extracciones que dejaron el agua al menos 1,5 °C por debajo de la temperatura mínima de confort |
| Demanda de agua caliente hoy | Lo extraído hoy más lo que necesita un resto de día fuerte (P90); atributos: el día habitual (P50), lo extraído (`observed`, `observed_liters`), el resto de hoy y el día completo del perfil (`profile_p50`/`profile_p90`, y en litros con caudalímetro) |
| Previsión de demanda de agua caliente | Demanda de las próximas 36 horas (P90), con las franjas de 15 minutos como atributo |
| Coeficiente de pérdidas (diagnóstico, desactivado por defecto) | UA aprendido, en W/K |
| Potencia de calentamiento aprendida (diagnóstico, desactivado por defecto) | Potencia media de la resistencia a plena potencia |
| Temperatura de corte del termostato (diagnóstico, desactivado por defecto) | Mediana de los últimos 20 cortes |
| Factor de capacidad efectiva (diagnóstico, desactivado por defecto) | Parte de la energía de calentamiento que ve la sonda |
| Temperatura del agua de red (diagnóstico) | Valor en uso y su origen |

El diagnóstico incluye los parámetros aprendidos, el resultado de la
importación del histórico, los días observados del perfil de uso y las
últimas 50 extracciones.

## Plan de calentamiento

Cada 15 minutos, y cuando cambia algo relevante, TankWise calcula el plan de
calentamiento más barato para las próximas 36 horas: cuánta energía de red
meter en cada franja y si aprovechar el excedente solar o dejarlo ir a la red
(cuando la inyección se paga más de lo que cuesta calentar de noche). Cada
cita debe empezar con litros útiles suficientes para todo su bloque; el resto
de la demanda se cubre franja a franja más una reserva. Una extracción suelta
pequeña (menos que la reserva antes de una hora más barata, como un grifo
tarde por la noche) no hace calentar en una hora cara. Las rachas de
calentamiento de red duran como mucho dos horas. Cuando el plan está en
riesgo, el atributo `risks` de `sensor.tankwise_plan` indica dónde (inicio,
fin), por qué (`appointment` o `demand`) y cuántos litros faltan.

**Recarga de rescate** (opción, activada por defecto): si el bloque de una
cita necesita más que el depósito lleno, se llena antes del bloque y, dentro
de él, solo se calienta la energía que falta para cubrir la demanda habitual
que queda, nunca después del bloque. Los rescates se registran y se cuentan
por mes (kWh y €). El plan en riesgo solo se enciende si ni con el rescate se
cubre la cita. Con la opción desactivada (modo estricto) nunca se calienta
dentro de un bloque.

| Modo | Comportamiento |
|------|----------------|
| Auto | Demanda habitual de un día fuerte (P90) |
| Eco | Demanda habitual de un día normal (P50): más barato, más riesgo |
| Confort | P90 más un 20 % |
| Vacaciones | Sin calentar de red, solo excedente solar |
| Sombra (por defecto) | Publica el plan sin actuar |
| Desactivado | Sin plan y sin actuar |

En modo Sombra compara el **Coste planificado** con el **Coste del termo**
(real) y mira el logbook: allí se registran las órdenes externas sobre el modo
manual que contradicen el plan.

| Entidad | Descripción |
|---------|-------------|
| Plan de calentamiento | Coste del plan para las próximas 36 horas, con sus calentamientos de red y los bloques de las citas como atributos. El plan franja a franja es demasiado grande para un atributo: está en el diagnóstico y en el servicio `tankwise.get_plan` |
| Próximo calentamiento | Inicio del próximo calentamiento de red, con duración, energía y coste |
| `<cita>` temperatura prevista | Temperatura que el plan prevé al empezar el bloque |
| Coste del termo hoy / este mes | Coste real: red al precio de compra, excedente a su valor |
| Coste planificado hoy / este mes | Lo que gasta el plan |
| Ahorro estimado este mes | Coste real menos coste planificado |
| Energía / coste de las recargas de rescate este mes | kWh y € de los rescates |
| Calentamiento de red planificado (binario) | La franja actual del plan calienta de red |
| Modo (select), reserva y valor del excedente (números), tope de compensación (interruptor), replanificar (botón) | Ajustes |

## Control y seguridades

En los modos Auto, Eco, Confort y Vacaciones, TankWise pone el regulador en
manual (plena potencia de red) durante los minutos planificados de cada
franja y lo devuelve a automático (solo excedente) después. Cada orden se
registra en el logbook con su motivo. Las órdenes se envían cuando el plan
cambia de decisión (y se repiten si no surten efecto), así que una orden
externa se respeta hasta el siguiente cambio; las seguridades se le siguen
aplicando.

| Situación | Acción |
|-----------|--------|
| Depósito por encima de la temperatura máxima de seguridad (número, 72 °C) durante 1 minuto en manual | Vuelta a automático; sin calentar de red hasta que se enfríe 3 °C |
| Sin potencia durante 3 minutos en manual | Corte del termostato: vuelta a automático, misma pausa |
| Más de 2 horas seguidas en manual | Vuelta a automático durante 15 minutos |
| Sonda no disponible o sin actualizar durante 10 minutos | Vuelta a automático, modo degradado, sin calentar de red |
| Regulador no disponible más allá de un margen (2 minutos, opción avanzada) mientras TankWise lo tiene en manual, o mientras la resistencia calienta fuera del plan | Se abre el interruptor del termo como último recurso; cuando el regulador vuelve se pone en automático y se cierra de nuevo el interruptor. Aparece un aviso de reparación si pasa tres veces en una semana |
| Interruptor del termo apagado (no por TankWise) | No se actúa; aviso de reparación |
| Otras automatizaciones activas cambian el modo manual del regulador, su salida o el interruptor del termo | Aviso de reparación con la lista (se pelearían con el controlador) |
| Cambio a modo Sombra o Desactivado | Se devuelve el regulador en automático si TankWise lo tenía en manual |

**Modo degradado** (sensor binario): sin plan (sin precios, o con el modelo
del depósito sin preparar) TankWise sigue una regla simple. Antes de cada
cita activa calienta hasta la temperatura que necesita (limitada al corte
aprendido del termostato), empezando con la antelación que requiere la
potencia aprendida. Solo si no hay citas activas usa dos ventanas fijas:
hasta 50 °C de 04:00 a 06:15 y hasta el corte del termostato de 15:00 a
16:00. Las seguridades son las mismas.

Cuando el plan pasa a estar en riesgo por un motivo nuevo, TankWise lanza el
evento `tankwise_plan_at_risk` (`reasons`, `new_reasons`, `appointments`),
por ejemplo para enviar una notificación cuando el regulador no está
disponible antes de una cita que depende de un calentamiento planificado.

## Boost

Un Boost significa "quiero agua caliente ahora", sea cual sea el motivo:
visitas, un baño, un día de mucho uso. El regulador pasa a manual al
instante, sin mirar el plan ni el precio, en todos los modos menos
Desactivado (también en Sombra y Vacaciones). Termina al llegar al
**objetivo del Boost** (número, 60 °C), al corte del termostato, tras la
**duración máxima** opcional (número, 90 minutos, 0 sin límite), cuando una
seguridad corta el calentamiento o al cancelarlo. Las seguridades no se
saltan nunca. Después se vuelve a calcular el plan con la energía ya
almacenada. Si el agua ya está por encima del objetivo no hace nada y lo
indica en el logbook.

| Entidad / servicio | Descripción |
|--------------------|-------------|
| Boost (botón) | Inicia un Boost; pulsado mientras está activo, lo cancela (un único pulsador sirve para las dos cosas) |
| Cancelar Boost (botón) | Cancela el Boost |
| Boost activo (binario) | Con inicio, objetivo, fin previsto, fin máximo, energía y coste |
| Objetivo del Boost, duración máxima del Boost (números) | Valores por defecto del botón |
| `tankwise.boost` | `temperature` o `liters` (litros a la temperatura de ducha que deben estar disponibles) y `duration` (minutos), opcionales |
| `tankwise.cancel_boost` | Cancela el Boost |

Los eventos `tankwise_boost_started` y `tankwise_boost_finished` (con
`reason`: `target`, `thermostat`, `duration`, `safety` o `cancelled`,
energía y coste) sirven para notificaciones o paneles.

Por defecto un Boost no cambia el perfil de uso, porque el motivo puede ser
cualquiera; las extracciones posteriores se aprenden con normalidad. La
opción **Contar el Boost como falta de agua** sube la previsión alrededor de
la hora de cada Boost, como tras una falta de agua.

### Pulsadores

Cualquier dispositivo puede iniciar un Boost con una automatización que
pulse `button.tankwise_boost` o llame a `tankwise.boost`. El blueprint
[`boost_from_button.yaml`](blueprints/automation/tankwise/boost_from_button.yaml)
lo hace sin escribir YAML: eliges el disparador (un disparador de
dispositivo de un pulsador ZHA, Zigbee2MQTT, Z-Wave o Shelly, un evento...),
la acción (alternar, solo activar o solo cancelar) y, si quieres, una
temperatura y una duración. Se importa desde **Ajustes → Automatizaciones y
escenas → Blueprints → Importar blueprint** con su URL de GitHub.

## Excedente solar y antilegionela

**Previsión solar.** En las entradas se eligen una o varias entradas de
previsión (Forecast.Solar, Solcast u Open-Meteo Solar Forecast: la misma
previsión horaria que usa el panel de Energía; varias, p. ej. una por
orientación del tejado, se suman). El plan cuenta con el excedente esperado:
la previsión menos lo que consume la casa en cada hora. Esa parte se aprende
para cada hora del día a partir de la exportación a red y de lo que el termo
tomó del excedente, así que también corrige el sesgo de la previsión. Hasta
aprender una hora se suponen 0,25 kWh de consumo de la casa.

**Vender el excedente.** Con el interruptor de salida del regulador ("PWM
enabled" de FreeDS) entre las entradas, cuando la inyección se paga más de
lo que cuesta calentar después (al menos 0,03 €/kWh más, por la
incertidumbre de la previsión), el plan deja el excedente para la red: se
apaga la salida, y se vuelve a encender después y antes de cualquier
calentamiento. Queda registrado en el logbook.

**Coordinación con otras cargas (SolarSplit).** El sensor **Potencia de
excedente pedida** (W) indica cuánto excedente quiere TankWise ahora, con
una prioridad (`priority`): `high` cuando el plan necesita ese excedente
para el agua caliente (sin él calentaría más de red antes de la siguiente
cita), `low` cuando solo llena el depósito. El evento
`tankwise_power_request` se lanza cuando cambia cualquiera de los dos, para
que un gestor de excedentes lo reparta entre cargas.

**Antilegionela** (interruptor, activado por defecto). Un ciclo es estar a
60 °C o más durante una hora, medido por la sonda; el sensor **Último ciclo
antilegionela** muestra cuándo fue y la fecha límite (7 días después). Dos
días antes de la fecha límite, el plan añade un objetivo de 61,5 °C
(limitado por debajo del corte del termostato) tras la hora en la que
calentar es más barato: con excedente primero y, si no, al precio de compra
más bajo. Si el depósito pasa una hora a 60 °C por sí solo (p. ej. en días
de sol), no se calienta nada de más.

## Servicios

| Servicio | Descripción |
|----------|-------------|
| `tankwise.replan` | Calcula ahora el plan de calentamiento. `config_entry_id` opcional. |
| `tankwise.get_plan` | Devuelve el plan de calentamiento completo, franja a franja (solo respuesta). `config_entry_id` opcional. |
| `tankwise.boost` / `tankwise.cancel_boost` | Inicia o cancela un Boost (ver Boost). |
| `tankwise.reimport_history` | Vuelve a importar el histórico del recorder de los últimos días y lo combina con lo aprendido (también el botón "Importar de nuevo el histórico"). `config_entry_id` opcional. |
| `tankwise.reset_learning` | Olvida las pérdidas de calor (`part: losses`), el perfil de uso (`part: demand`) o todo (`part: all`, por defecto). `config_entry_id` opcional. |
| `tankwise.discard_last_cutoff_sample` | Olvida la última temperatura de corte del termostato aprendida y vuelve a calcular el corte. `config_entry_id` opcional; devuelve la muestra quitada y las que quedan. |
| `tankwise.discard_cutoff_sample` | Igual para una muestra, indicada por `value` (°C, la más cercana a menos de 0,5 °C) o `index` (0 = la más antigua, −1 = la más reciente). Las muestras son el atributo `samples` del sensor de temperatura de corte. |

## Cómo funciona el modelo del depósito

- **Energía y litros.** `C = V · 0,001163` kWh/K. Energía almacenada
  `E = C · (T − T_red)`. El agua fría que entra se mezcla con la caliente, así
  que solo se aprovecha el calor por encima de la temperatura de confort:
  litros útiles a la temperatura de ducha
  `L = k_use · V · (T − T_confort) / (T_ducha − T_red)`, con `k_use` aprendido
  de las faltas reales. Cuando el caudalímetro ha enseñado la estratificación
  (al menos 3 muestras y la sonda bajando como mucho el 80 % de un depósito
  mezclado), el agua sale caliente mientras el agua fría llena el fondo, así
  que es útil la energía sobre el **agua de red**, por `k_use` (se pierde la
  zona de mezcla): `L = k_use · V · (T − T_red) / (T_ducha − T_red)`. El mismo
  límite se usa en las citas, el plan y su riesgo.
- **Pérdidas.** `P = UA · (T − T_ambiente)`. UA se aprende por mínimos
  cuadrados recursivos con la pendiente de la sonda en ventanas de reposo de
  al menos dos horas (sin calentar ni extracciones), entre 0,3 y 3 W/K.
- **Capacidad efectiva.** La sonda está arriba y no ve toda la energía. El
  factor `k_cap` (0,9 al principio) se aprende comparando la energía de
  calentamiento con la subida de la sonda en calentamientos sin extracciones.
- **Potencia y termostato.** La potencia real de la resistencia se aprende del
  sensor de potencia. Un corte del termostato se detecta cuando el regulador
  pide potencia (modo manual encendido o PWM por encima del 10 %) y la
  potencia se queda por debajo de 50 W durante 3 minutos; ese instante se toma
  de los cambios de estado, así que una automatización que apague el modo
  manual después no lo oculta. No es un corte si, desde un minuto antes de la
  caída hasta tres minutos después, el regulador no estuvo disponible o se
  reinició (sus entidades tardan unos dos minutos en aparecer como no
  disponibles), o si, desde un minuto antes hasta dos minutos después, estuvo
  en un modo que no calienta o pidió un 0 % (un corte de alimentación del
  derivador también para la resistencia). Mientras no se ha
  aprendido ningún corte, debe estar a menos de 12 °C por debajo del
  configurado; después, a menos de 4 °C del corte conocido.
- **Extracciones.** Cada minuto se compara el cambio observado de la sonda
  filtrada con el previsto. Una extracción empieza cuando el residuo acumulado
  en 10 minutos baja de −1 °C y termina cuando se mantiene cerca de cero
  durante 5 minutos. Su energía es `C · ΔT_residual / k_cap`.
- **Caudalímetro de agua caliente.** Con un caudalímetro a la salida las
  extracciones se miden, no se estiman: empiezan con el primer litro y
  terminan tras 3 minutos sin otro (los pulsos sueltos se agrupan; mientras el
  caudal es mayor que cero sigue abierta). Su energía es
  `litros · 1,163 Wh/(L·K) · (T_salida − T_red)`, con `T_salida` la
  temperatura del termo al empezar: en un depósito estratificado el agua sale
  caliente casi toda la extracción, así que la bajada de la sonda no la
  rebaja. Los litros se cuentan aunque la sonda no esté disponible. El modelo
  aprende la estratificación de la pareja «energía medida / bajada de la
  sonda» (extracciones que bajarían un depósito mezclado al menos 4 °C, sin
  calentar): qué parte de la bajada llega a la sonda, según la temperatura.
  Con ella el modelo conserva la energía que salió aunque la sonda de arriba
  apenas baje, y un calentamiento hasta el corte vuelve a mezclar el
  depósito. La sonda solo corrige el modelo más allá de 1 °C (resolución y
  ruido de un termopar). La reconciliación energética nunca escala las
  extracciones medidas, así que `k_use` se aprende con energías exactas. Sin
  caudalímetro no cambia nada.
- **Perfil de uso.** Cada día completo (al menos 20 h observadas, casa
  ocupada) se guarda como la energía extraída en sus 96 franjas de 15
  minutos (y los litros, si un caudalímetro midió el día entero). La previsión usa los últimos 16 días del mismo tipo, con más peso
  los recientes (semivida de tres semanas). Los percentiles se calculan sobre
  la curva acumulada de cada día, no franja a franja: la P90 de un día es un
  día "fuerte", y las duchas que se mueven 15–30 minutos no cuentan dos
  veces. Una extracción que dejó el agua por debajo de la temperatura de
  confort sube un 20 % la previsión de esa hora durante dos semanas. Con la
  casa vacía la previsión es cero.
- **Reconciliación energética.** La sonda de arriba ve una ducha tarde y
  pequeña mientras el agua fría se queda abajo, y demasiado grande cuando el
  frente frío llega a ella. Tras cada calentamiento el depósito está mezclado,
  así que el balance de energía entre dos calentamientos asentados da la
  energía realmente extraída y las extracciones intermedias se ajustan a ella
  (solo las estimadas).
  Una bajada de más de 1 °C sobre las pérdidas en una hora en reposo se
  registra como extracción lenta.
- **Falta de agua caliente.** Una extracción que llevó la sonda al menos
  1,5 °C por debajo de la temperatura mínima de confort.
- **Importación del histórico.** En el primer arranque se reproducen los
  últimos 10 días del recorder a través de los modelos, así que pérdidas,
  cortes, extracciones y el perfil de uso se conocen desde el primer día. Al
  añadir un caudalímetro se importan de nuevo: las extracciones de esos días
  se miden con su histórico.
- **Modo seguro.** Sin sonda o sin potencia el modelo se congela y los
  sensores del depósito quedan no disponibles; no se inventa nada. Tras una
  interrupción de más de 30 minutos el estado es incierto hasta el siguiente
  llenado. Una bajada de la sonda durante la interrupción que no explican las
  pérdidas se registra como extracción estimada (`estimated` en
  `sensor.tankwise_last_draw`). Que el regulador no esté disponible no es una
  interrupción del modelo del depósito.

## Documentación

- [Migración desde las automatizaciones](docs/migration.md) (en inglés): modo
  Sombra, comparación, paso a Auto y limpieza.
- [Arquitectura](docs/architecture.md) (en inglés): cómo funcionan el modelo
  del depósito, la previsión de demanda, el optimizador y el controlador.

## Desarrollo

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest
```

Los modelos de `custom_components/tankwise/model/` son Python puro y no
importan Home Assistant.

### Exportar el histórico para el banco de simulación

`tools/export_history.py` descarga el histórico de las entradas mediante la
API REST de Home Assistant a `tests/fixtures/` (excluido de git). La URL y un
token de acceso de larga duración se leen de las variables de entorno
`HA_URL` y `HA_TOKEN` o de un fichero `.env` (también excluido de git):

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...
python tools/export_history.py --start 2026-11-07 --end 2026-11-20 \
  --entity temperature=sensor.mi_termo_temperatura \
  --entity power=sensor.mi_termo_potencia
```

`python tools/export_history.py --help` muestra todos los roles.

## Licencia

[MIT](LICENSE)
