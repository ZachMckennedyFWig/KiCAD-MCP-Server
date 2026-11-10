/**
 * Library resources for KiCAD MCP server
 *
 * These resources provide information about KiCAD component libraries
 * to the LLM, enabling better context-aware assistance.
 */

import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";

// Command function type for KiCAD script calls
type CommandFunction = (command: string, params: Record<string, unknown>) => Promise<any>;

/**
 * Register library resources with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerLibraryResources(
  server: McpServer,
  callKicadScript: CommandFunction,
): void {
  logger.info("Registering library resources");

  // ------------------------------------------------------
  // Component Library Resource -- footprint search across the fp-lib-table
  // (backed by search_footprints; filter is a case-insensitive pattern, * = any)
  // ------------------------------------------------------
  server.resource(
    "component_library",
    new ResourceTemplate("kicad://components/{filter?}/{library?}", {
      list: undefined,
    }),
    async (uri, params) => {
      const filter = params.filter || "";
      const library = params.library || "";
      const limit = Number(params.limit) || undefined;

      logger.debug(
        `Retrieving component library${filter ? ` with filter: ${filter}` : ""}${library ? ` from library: ${library}` : ""}`,
      );

      const result = await callKicadScript("search_footprints", {
        pattern: filter || "*",
        library: library || undefined,
        limit,
      });

      if (!result.success) {
        logger.error(`Failed to retrieve component library: ${result.errorDetails}`);
        return {
          contents: [
            {
              uri: uri.href,
              text: JSON.stringify({
                error: "Failed to retrieve component library",
                details: result.errorDetails,
              }),
              mimeType: "application/json",
            },
          ],
        };
      }

      logger.debug(
        `Successfully retrieved ${result.footprints?.length || 0} footprints from library`,
      );
      return {
        contents: [
          {
            uri: uri.href,
            text: JSON.stringify(result),
            mimeType: "application/json",
          },
        ],
      };
    },
  );

  // ------------------------------------------------------
  // Library List Resource
  // ------------------------------------------------------
  server.resource("library_list", "kicad://libraries", async (uri) => {
    logger.debug("Retrieving library list");
    const result = await callKicadScript("list_libraries", {});

    if (!result.success) {
      logger.error(`Failed to retrieve library list: ${result.errorDetails}`);
      return {
        contents: [
          {
            uri: uri.href,
            text: JSON.stringify({
              error: "Failed to retrieve library list",
              details: result.errorDetails,
            }),
            mimeType: "application/json",
          },
        ],
      };
    }

    logger.debug(`Successfully retrieved ${result.libraries?.length || 0} libraries`);
    return {
      contents: [
        {
          uri: uri.href,
          text: JSON.stringify(result),
          mimeType: "application/json",
        },
      ],
    };
  });

  // ------------------------------------------------------
  // Footprint Resource (backed by get_footprint_info)
  // kicad://footprint/Resistor_SMD:R_0805_2012Metric or
  // kicad://footprint/Resistor_SMD/R_0805_2012Metric
  // ------------------------------------------------------
  server.resource(
    "component_footprint",
    new ResourceTemplate("kicad://footprint/{componentId}/{footprint?}", {
      list: undefined,
    }),
    async (uri, params) => {
      const { componentId, footprint } = params;
      logger.debug(
        `Retrieving footprint for component: ${componentId}${footprint ? ` (${footprint})` : ""}`,
      );

      const result = await callKicadScript("get_footprint_info", {
        footprint_name: footprint ? `${componentId}:${footprint}` : componentId,
      });

      if (!result.success) {
        logger.error(`Failed to retrieve component footprint: ${result.errorDetails}`);
        return {
          contents: [
            {
              uri: uri.href,
              text: JSON.stringify({
                error: `Failed to retrieve footprint for component ${componentId}`,
                details: result.errorDetails,
              }),
              mimeType: "application/json",
            },
          ],
        };
      }

      logger.debug(`Successfully retrieved footprint for component: ${componentId}`);
      return {
        contents: [
          {
            uri: uri.href,
            text: JSON.stringify(result),
            mimeType: "application/json",
          },
        ],
      };
    },
  );

  // ------------------------------------------------------
  // Symbol Resource (backed by get_symbol_info), e.g. kicad://symbol/Device:R
  // ------------------------------------------------------
  server.resource(
    "component_symbol",
    new ResourceTemplate("kicad://symbol/{componentId}", {
      list: undefined,
    }),
    async (uri, params) => {
      const { componentId } = params;
      logger.debug(`Retrieving symbol for component: ${componentId}`);

      const result = await callKicadScript("get_symbol_info", {
        symbol: componentId,
      });

      if (!result.success) {
        logger.error(`Failed to retrieve component symbol: ${result.errorDetails}`);
        return {
          contents: [
            {
              uri: uri.href,
              text: JSON.stringify({
                error: `Failed to retrieve symbol for component ${componentId}`,
                details: result.errorDetails,
              }),
              mimeType: "application/json",
            },
          ],
        };
      }

      logger.debug(`Successfully retrieved symbol for component: ${componentId}`);

      // If the result includes SVG data, return it as SVG
      if (result.svgData) {
        return {
          contents: [
            {
              uri: uri.href,
              text: result.svgData,
              mimeType: "image/svg+xml",
            },
          ],
        };
      }

      // Otherwise return the JSON result
      return {
        contents: [
          {
            uri: uri.href,
            text: JSON.stringify(result),
            mimeType: "application/json",
          },
        ],
      };
    },
  );

  logger.info("Library resources registered");
}
