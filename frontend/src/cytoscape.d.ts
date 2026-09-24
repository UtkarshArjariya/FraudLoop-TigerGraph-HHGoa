declare module "react-cytoscapejs" {
  import type { ComponentType, CSSProperties } from "react";
  import type {
    Core,
    ElementDefinition,
    LayoutOptions,
    StylesheetJson,
  } from "cytoscape";
  const CytoscapeComponent: ComponentType<{
    elements: ElementDefinition[];
    stylesheet?: StylesheetJson;
    layout?: LayoutOptions;
    style?: CSSProperties;
    cy?: (cy: Core) => void;
    minZoom?: number;
    maxZoom?: number;
    wheelSensitivity?: number;
  }>;
  export default CytoscapeComponent;
}
