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

> **Desarrollo inicial (0.1.0, fase 1).** La integración se puede instalar y
> configurar, y lee los sensores del termo cada 60 segundos. **Todavía no**
> crea ninguna entidad.

| Fase | Contenido | Estado |
|------|-----------|--------|
| 1 | Esqueleto, config flow, lectura de sensores | ✅ Hecha |
| 2 | Modelo del depósito (estado de carga, recalibración, aprendizaje de pérdidas) | Pendiente |
| 3 | Detección de consumos y perfil aprendido | Pendiente |
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

**Opciones (parámetros del termo)**

| Parámetro | Valor por defecto |
|-----------|-------------------|
| Volumen | 100 L |
| Potencia nominal | 1450 W |
| Zona del agua de red | Islas Baleares |
| Temperatura del agua de red | Valor mensual de la zona (se puede fijar a mano) |
| Temperatura útil mínima | 38 °C |
| Umbral de potencia de corte del termostato | 50 W |
| Nivel de confort | P90 |

Las tablas de temperatura del agua de red (Islas Baleares, Península genérica
e Islas Canarias) son medias mensuales aproximadas a partir de las tablas de
referencia españolas para el dimensionado de agua caliente sanitaria.

## Licencia

[MIT](LICENSE)
