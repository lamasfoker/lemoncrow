/**
 * Public, source-consumed UI package for both LemonCrow compositions.
 * Runtime-specific identity, data adapters and administration belong to callers.
 */
export { default as WorkbenchApp } from "./App";
export type { PageComposition, ReviewComposition, ShellComposition } from "./App";
export { PageFrame } from "./components/WorkbenchUI";
export { Button } from "./components/ui/button";
export { Input } from "./components/ui/input";
export {
  DiscoveredProductCapabilitiesProvider,
  ProductCapabilitiesProvider,
  RuntimeCapabilitiesProvider,
  LOCAL_PRODUCT_CAPABILITIES,
  LOCAL_RUNTIME_CAPABILITIES,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
  RUNTIME_ONLY_PRODUCT_CAPABILITIES,
  CORE_ONLY_PRODUCT_CAPABILITIES,
  createProductCapabilities,
  fetchProductCapabilities,
  parseProductCapabilities,
} from "./lib/RuntimeCapabilities";
export type {
  LemonCrowCapabilities,
  ProductFeature,
  ProductId,
  ProductMode,
  ProductState,
  RuntimeCapabilities,
} from "./lib/RuntimeCapabilities";
