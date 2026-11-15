<p align="center">
  <img src="images/banner.png" alt="TankWise" width="100%">
</p>

# TankWise

[![Validate](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml)
[![Tests](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml)

*[Read in English](README.md)*

TankWise es una integración de Home Assistant que modela de forma continua un
termo eléctrico de acumulación y el uso de agua caliente de la casa. Publica
**cuánta energía necesita el termo y para cuándo**, para que siempre haya agua
caliente cuando se va a usar, con el mínimo coste de red.

**TankWise no actúa sobre ningún dispositivo.** No enciende relés ni cambia el
modo de un derivador de excedentes. Solo publica la demanda; otra integración
o tus propias automatizaciones deciden cómo cubrirla.

## Estado del proyecto

> **Desarrollo inicial (0.2.2, fase 2).** TankWise modela el depósito y
> publica su estado de carga. El aprendizaje del uso y el plan de
> calentamiento (la salida para otras integraciones) todavía no están
> disponibles.

| Fase | Contenido | Estado |
|------|-----------|--------|
| 1 | Esqueleto, config flow, lectura de sensores | ✅ Hecha |
| 2 | Modelo del depósito (estado de carga, recalibración, aprendizaje de pérdidas) | ✅ Hecha |
| 3 | Detección de consumos y perfil aprendido | En curso |
| 4 | Planificador y contrato de salida | Pendiente |
| 5 | Botón de refuerzo, selector de confort, modo vacaciones, diagnósticos | Pendiente |
| 6 | Documentación completa y primera versión estable | Pendiente |

## Instalación

1. En HACS, abre el menú y elige **Repositorios personalizados**.
2. Añade `https://github.com/jdgarcia99/ha-tankwise` con la categoría **Integración**.
3. Instala **TankWise** y reinicia Home Assistant.
4. Ve a **Ajustes → Dispositivos y servicios → Añadir integración → TankWise**.

## Configuración

Todo se configura desde la interfaz; no hay nada en `configuration.yaml`.

**Sensores obligatorios**

- Temperatura del termo (°C o °F).
- Potencia de la resistencia (W o kW).
- Energía de la resistencia (kWh o Wh, con clase de estado `total_increasing` o `total`).

**Entradas opcionales**

- Previsión solar (Forecast.Solar o Solcast). En esta versión solo se guarda
  la selección: la curva horaria de previsión (horizonte de 36 h) se leerá
  cuando se implemente el planificador (fase 4).
- Potencia de excedente solar actual.
- Precio de la electricidad (por ejemplo, PVPC).
- Interruptor de presencia o «casa activada»: si está apagado, TankWise pasa a
  modo vacaciones.
- Sonda de temperatura de la parte baja del termo.
- Modo manual del regulador: el interruptor que obliga al regulador a
  calentar (por ejemplo, el modo manual del derivador). Si lo enciende una
  persona, cuenta como fallo de previsión. Las automatizaciones solo
  cuentan si están en la lista de *automatizaciones que actúan por una
  persona* (por ejemplo, un pulsador físico).
- Sensor de día laborable (integración Workday), para distinguir los
  festivos.
- Salida del regulador (%): salida del regulador que gobierna la resistencia,
  por ejemplo la salida PWM de un derivador de excedentes. Si existe, es el
  criterio principal para detectar el depósito lleno (el regulador pide
  potencia y la resistencia no consume: el termostato está abierto).

**Opciones (parámetros del termo)**

| Parámetro | Valor por defecto |
|-----------|-------------------|
| Volumen | 100 L |
| Potencia nominal | 1450 W |
| Zona del agua de red | Islas Baleares |
| Temperatura del agua de red | Valor mensual de la zona (se puede fijar a mano) |
| Temperatura mínima útil (medida en la sonda) | 32 °C |
| Umbral de potencia de corte del termostato | 50 W |
| Temperatura de corte del termostato (valor inicial, después se aprende) | 47 °C |
| Energía por ducha | 1,8 kWh |
| Nivel de confort | P90 |

Las tablas de temperatura del agua de red (Islas Baleares, Península genérica
e Islas Canarias) son medias mensuales aproximadas a partir de las tablas de
referencia españolas para el dimensionado de agua caliente sanitaria.

## Entidades

| Entidad | Descripción |
|---------|-------------|
| Energía útil | kWh almacenados por encima de la temperatura mínima útil |
| Carga | Energía útil en porcentaje del depósito lleno |
| Duchas disponibles | Energía útil dividida entre la energía por ducha |
| Estado del modelo | `calibrated` (calibrado), `learning` (aún aprendiendo las pérdidas) o `uncertain` (incierto: sin recalibrar desde el arranque o tras una interrupción larga) |
| Próximo consumo previsto | Cuándo se espera el siguiente uso de agua caliente, con su energía y probabilidad |
| Perfil de demanda | `learning` (aprendiendo: menos de 14 días de datos) o `ready` (listo) |

El coeficiente de pérdidas aprendido, la temperatura de corte aprendida y los
últimos consumos detectados están en los diagnósticos de la integración.

Si un sensor obligatorio deja de estar disponible, el modelo se congela y las
entidades pasan a no disponibles hasta que se recupere; no se inventan
consumos.

## Contrato de salida

Estas son las entidades que deben usar otras integraciones (por ejemplo,
SolarSplit) y las automatizaciones para cubrir la demanda del termo. Se crean
en la fase 4 y forman un contrato estable y documentado: su significado y sus
unidades no cambiarán sin una versión mayor.

| Entidad | Tipo | Significado |
|---------|------|-------------|
| Energía necesaria | `sensor`, kWh | Energía que debe entrar en el termo antes del plazo |
| Plazo | `sensor`, marca de tiempo | Plazo del siguiente objetivo |
| Red permitida | `binary_sensor` | Si en este momento se permite calentar con red según el plan |
| Potencia pedida | `sensor`, W | Potencia pedida ahora (`0` = solo excedente) |
| Urgencia | `sensor`, enum | `none`, `planned` o `urgent` |

TankWise dispara además un evento `tankwise_plan_updated` con el plan completo
cada vez que cambia.

Los `entity_id` **no** son fijos: salen del nombre que le des a la instancia
de TankWise (por ejemplo, `sensor.<nombre>_energy_needed`) y se pueden
renombrar en Home Assistant. Quien los use debe dejar que el usuario elija
estas entidades (como hace SolarSplit en su config flow) en lugar de escribir
los `entity_id` a mano.

## Perfil de uso

TankWise aprende cuándo se usa el agua caliente a partir de los consumos que
detecta:

- Franjas de 15 minutos para laborables, sábados y domingos/festivos (con el
  sensor Workday, un festivo entre semana usa el perfil de domingo; los
  sábados son siempre sábados).
- Las últimas semanas pesan más (media vida de 21 días).
- Las previsiones usan el percentil de confort (P80/P90/P95) de la demanda
  acumulada de los días anteriores, así cubren la demanda diaria real sin
  sumar los extremos de cada franja.
- Los fallos de previsión hacen que esos días pesen más: un consumo empezado
  con menos energía útil que media ducha, o el modo manual del regulador
  encendido por una persona. Una ducha fuerte que deja el termo por debajo de
  la temperatura mínima útil es normal, no un fallo.
- Con menos de 14 días de datos se mezcla un perfil por defecto.
- La energía por ducha se aprende en cuanto hay al menos cinco duchas.

### Importar histórico

Para saltarse el periodo de aprendizaje, importa lecturas en bruto minuto a
minuto con la acción `tankwise.import_history` (solo administradores). El CSV
necesita marca de tiempo, temperatura del termo, potencia y energía de la
resistencia y, opcionalmente, la salida del regulador (%), el estado del modo
manual (`on`/`off`) y `manual_source` (quién encendió el modo manual: `user`,
`automation:<entity_id>` o vacío). Admite separador coma, punto y coma o tabulador, y coma
decimal. Las lecturas se pasan por el modelo del depósito, así que los
consumos y los fallos se detectan igual que en vivo; se toleran huecos y
valores `unavailable`. Los días que ya están en el histórico se omiten. El
archivo debe estar dentro de la carpeta de configuración (o de un directorio
externo permitido).

```yaml
action: tankwise.import_history
data:
  config_entry_id: <tu instancia de TankWise>
  file_path: /config/tankwise_history.csv
```

Una activación del modo manual cuenta como fallo de previsión con el mismo
criterio que en vivo: si `manual_source` es `user` o una de las
*automatizaciones que actúan por una persona*. En los archivos sin columna
`manual_source` las activaciones no cuentan, salvo que se indique
`manual_mode_is_failure: true`.

### Exportar el histórico desde Home Assistant

`tools/export_history.py` descarga los últimos N días de la API REST de Home
Assistant (historial y registro) y escribe el CSV en el formato que espera
`tankwise.import_history`, una fila por minuto e incluida `manual_source`. Solo
necesita Python 3.12 o posterior (únicamente la biblioteca estándar).

Crea un token de acceso de larga duración en tu perfil de Home Assistant y
pásalo en la variable de entorno `HA_TOKEN`. El token solo se envía a Home
Assistant; nunca se escribe en disco ni se muestra.

```bash
export HA_TOKEN="<token de larga duración>"   # PowerShell: $env:HA_TOKEN = "..."
python tools/export_history.py \
  --url http://homeassistant.local:8123 \
  --temperature sensor.termo_temperatura \
  --power sensor.termo_potencia \
  --energy sensor.termo_energia \
  --regulator sensor.derivador_pwm \
  --manual-mode switch.derivador_modo_manual \
  --days 10 \
  --output tankwise_history.csv
```

Copia el archivo a la carpeta de configuración (por ejemplo,
`/config/tankwise_history.csv`) y ejecuta la acción. Las unidades se convierten
a °C, W y kWh. El historial solo guarda los cambios, así que cada minuto lleva
el último valor conocido.

## Cómo funciona el modelo del depósito

- La sonda se filtra (mediana + media móvil) para que su oscilación de ±1 °C
  no parezca ni un consumo ni un calentamiento. El calentamiento solo se toma
  del contador de energía de la resistencia, nunca de la sonda.
- El estado de carga sigue un balance de energía: entra la energía de la
  resistencia y salen las pérdidas en reposo (que se aprenden despacio en los
  periodos tranquilos, empezando en 0,25 °C/h) y los consumos detectados.
- Un consumo es una caída sostenida de la sonda filtrada, también mientras
  calienta.
- El depósito se recalibra a «lleno» cuando corta el termostato: con sensor de
  regulador, cuando el regulador pedía potencia (más del 10 %) en el instante
  en que la resistencia dejó de consumir tras calentar de verdad, y la potencia
  sigue baja 3 minutos, aunque el regulador se apague después. Ese instante se
  toma de los cambios de estado, no solo de las lecturas cada 60 segundos; si
  el regulador bajó justo antes de que cayera la potencia, no es un lleno. Sin
  sensor de regulador, el calentamiento debe pararse cerca de la temperatura
  de corte.
- El estado se guarda y se restaura tras los reinicios. El calor que entró con
  Home Assistant parado se toma del contador de energía; tras una interrupción
  de más de 30 minutos el estado se marca como incierto hasta el siguiente
  llenado.

## Desarrollo

```bash
pip install -r requirements_test.txt
pytest
```

Los modelos de `custom_components/tankwise/model/` son Python puro y no
importan Home Assistant. El entorno de pruebas de Home Assistant no funciona en
Windows, así que en Windows ejecuta solo los tests del modelo:

```bash
pytest tests/model -p no:homeassistant
```

La batería completa se ejecuta en GitHub Actions.

## Licencia

[MIT](LICENSE)
