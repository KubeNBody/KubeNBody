// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// The d3 functions the charts use, from the d3 submodules: importing the `d3`
// bundle also pulls in d3-transition, which is marked side-effectful and so
// survives tree-shaking even though nothing here animates.
export { bisector, extent, max, min } from 'd3-array';
export { axisBottom, axisLeft } from 'd3-axis';
export { format } from 'd3-format';
export { scaleLinear, scaleLog } from 'd3-scale';
export { pointer, select } from 'd3-selection';
export { area, curveMonotoneX, line } from 'd3-shape';
export type { ScaleLinear } from 'd3-scale';
export type { NumberValue } from 'd3-scale';
