import { useSearchParams } from "react-router-dom";

import { useRuntimeCapabilities } from "../lib/RuntimeCapabilities";
import Sessions from "./Sessions";
import Swarms from "./Swarms";

export default function Runs() {
  const [searchParams] = useSearchParams();
  const capabilities = useRuntimeCapabilities();
  return capabilities.swarms && searchParams.get("type") === "swarms" ? <Swarms /> : <Sessions />;
}
