# Novedades de cada versión

> [中文](release-highlights.zh.md) · [English](release-highlights.md) · **Español**

Resumen orientado al usuario de lo que mejoró cada versión. Cada cifra debe poder
rastrearse hasta una entrada `### Validation` en [CHANGELOG.md](../CHANGELOG.md);
este archivo nunca introduce una cifra propia.

A partir de 0.5.0, una versión aparece aquí cuando modifica el rendimiento o el
comportamiento visible para el usuario, y se describe en comparación con la versión
anterior. Qué se compara depende del tipo de cambio: el trabajo de rendimiento se
compara con cifras medidas; el de comportamiento, con lo que la herramienta hace
ahora de forma distinta. Una versión que no altere ninguno de los dos no se lista.

## 0.5.0

Una versión de comportamiento, no de rendimiento. La puntuación pasó a depender del
CV, lo que cambia qué devuelve una búsqueda y cuánto cuesta. Las cifras proceden de
la entrada `### Validation` de `f14e369` → `b47dfc0`.

**Cambiar de CV vuelve a abrir todos los empleos en caché**
Hasta 0.4.0 la puntuación se guardaba sin registrar qué CV la produjo, así que un
empleo juzgado con un CV antiguo seguía juzgado. En una caché de 293 empleos, 95 que
un CV anterior había rechazado no podían reconsiderarse nunca; ahora son 0.

**`assess` dejó de informar de un trabajo que no hacía**
Filtraba por la columna heredada antes de consultar los resultados de coincidencia y,
como cada fila llevaba una puntuación heredada, anunciaba "todo evaluado" mientras 205
empleos carecían de puntuación para el CV actual. Empleos alcanzables: 0 → 205.

**Una sola escala de puntuación en lugar de dos**
Una puntuación heredada de 0–10 y una de coincidencia de 0–100 se devolvían por la
misma propiedad y se ordenaban entre sí. Escalas en `effective_score`: 2 → 1.

**Nuevo: `jobradar cache prune-scores`**
Elimina resultados de coincidencia de una versión de prompt obsoleta y, opcionalmente,
de un CV obsoleto, para que la siguiente búsqueda los recalcule.

**Nota sobre el coste**
Esta versión añade trabajo de evaluación en lugar de quitarlo. La primera búsqueda tras
cambiar de CV reevalúa los empleos en caché en vez de reutilizar el veredicto de otro CV;
las búsquedas posteriores no se ven afectadas. No se afirma ninguna mejora de latencia ni
de coste.

## 0.4.0

Líneas base: `bench/serial-baseline` para las cifras de rendimiento y
`bench/pre-merged-eval` → `bench/merged-eval` para las cifras de coste. Esta
versión abarca diez pull requests, por lo que no se compara con `v0.3.0`.

**La búsqueda es 2,6 veces más rápida**
118,5 s → 45,6 s (-61,5 %)

**El primer resultado llega 3,7 veces antes**
47,5 s → 12,8 s (-73,1 %)

**Rendimiento un 145 % mayor**
14,7 → 36,0 ofertas por minuto

**Llamadas al LLM reducidas a la mitad**
41 → 20 por ejecución (-51,2 %)

**Tokens de entrada por oferta un 47,5 % menos**
5.251 → 2.759
