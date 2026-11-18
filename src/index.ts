/**
 * KiCAD Model Context Protocol Server
 * Main entry point
 */

import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { KiCADMcpServer } from "./server.js";
import { resolveHttpOptions, TRANSPORT_ENV } from "./http-transport.js";
import { loadConfig } from "./config.js";
import { logger } from "./logger.js";

// Get the current directory
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Main function to start the KiCAD MCP server
 */
async function main() {
  try {
    // Parse command line arguments
    const args = process.argv.slice(2);
    const options = parseCommandLineArgs(args);

    // Load configuration
    const config = await loadConfig(options.configPath);

    // Path to the Python script that interfaces with KiCAD
    const kicadScriptPath = join(dirname(__dirname), "python", "kicad_interface.py");

    // Transport: stdio (default), or Streamable HTTP with --http or
    // KICAD_MCP_TRANSPORT=http (for clients that cannot start a process).
    const transport =
      options.http || (process.env[TRANSPORT_ENV] ?? "").trim().toLowerCase() === "http"
        ? "http"
        : "stdio";
    const http =
      transport === "http"
        ? resolveHttpOptions(process.env, { host: options.host, port: options.port })
        : undefined;

    // Create the server
    const server = new KiCADMcpServer(kicadScriptPath, config.logLevel, { transport, http });

    // Setup graceful shutdown before start(): an HTTP server is reachable as
    // soon as it listens.
    setupGracefulShutdown(server, transport);

    // Start the server
    await server.start();

    logger.info(
      `KiCAD MCP server started with ${transport === "http" ? "HTTP" : "STDIO"} transport`,
    );
  } catch (error) {
    logger.error(`Failed to start KiCAD MCP server: ${error}`);
    process.exit(1);
  }
}

/**
 * Parse command line arguments
 */
function parseCommandLineArgs(args: string[]) {
  let configPath = undefined;
  let http = false;
  let host: string | undefined;
  let port: number | undefined;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--config" && i + 1 < args.length) {
      configPath = args[i + 1];
      i++;
    } else if (args[i] === "--http") {
      http = true;
    } else if (args[i] === "--host" && i + 1 < args.length) {
      host = args[i + 1];
      http = true;
      i++;
    } else if (args[i] === "--port" && i + 1 < args.length) {
      port = Number(args[i + 1]);
      http = true;
      i++;
    }
  }

  return { configPath, http, host, port };
}

/**
 * Setup graceful shutdown handlers
 */
function setupGracefulShutdown(server: KiCADMcpServer, transport: "stdio" | "http") {
  // Over stdio, stdin closing means the client went away. An HTTP server may
  // run with no stdin at all (a service, a background job), so it ignores it.
  if (transport === "stdio") {
    process.stdin.on("close", async () => {
      logger.info("process.stdin closed. Shutting down...");
      await shutdownServer(server);
    });
  }

  // Handle termination signals
  process.on("SIGINT", async () => {
    logger.info("Received SIGINT signal. Shutting down...");
    await shutdownServer(server);
  });

  process.on("SIGTERM", async () => {
    logger.info("Received SIGTERM signal. Shutting down...");
    await shutdownServer(server);
  });

  // Handle uncaught exceptions
  process.on("uncaughtException", async (error) => {
    logger.error(`Uncaught exception: ${error}`);
    await shutdownServer(server);
  });

  // Handle unhandled promise rejections
  process.on("unhandledRejection", async (reason) => {
    logger.error(`Unhandled promise rejection: ${reason}`);
    await shutdownServer(server);
  });
}

/**
 * Shut down the server and exit
 */
async function shutdownServer(server: KiCADMcpServer) {
  try {
    logger.info("Shutting down KiCAD MCP server...");
    await server.stop();
    logger.info("Server shutdown complete. Exiting...");
    process.exit(0);
  } catch (error) {
    logger.error(`Error during shutdown: ${error}`);
    process.exit(1);
  }
}

// Run the main function - always run when imported as module entry point
// The import.meta.url check was failing on Windows due to path separators
main().catch((error) => {
  console.error(`Unhandled error in main: ${error}`);
  process.exit(1);
});

// For testing and programmatic usage
export { KiCADMcpServer };
