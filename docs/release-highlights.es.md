# Novedades de cada versión

> [中文](release-highlights.zh.md) · [English](release-highlights.md) · **Español**

Resumen orientado al usuario de lo que mejoró cada versión. Cada cifra debe poder
rastrearse hasta una entrada `### Validation` en [CHANGELOG.md](../CHANGELOG.md);
este archivo nunca introduce una cifra propia.

A partir de 0.5.0, cada versión se compara con la versión anterior.

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
