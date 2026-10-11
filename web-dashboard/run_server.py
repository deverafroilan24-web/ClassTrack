import argparse
import os
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="ClassTrack Web Dashboard - Cloud Deployable")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Server host IP")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument("--reload", action=argparse.BooleanOptionalAction, default=True, help="Enable uvicorn hot reload")
    parser.add_argument("--database", choices=("auto", "sqlite", "postgres"), default=None,
                        help="Select storage explicitly; local launchers use sqlite")
    args = parser.parse_args()
    if args.database:
        os.environ["DATABASE_MODE"] = args.database
    elif args.host in {"127.0.0.1", "localhost", "::1"}:
        os.environ["DATABASE_MODE"] = "sqlite"

    print(f"============================================================")
    print(f"  ClassTrack Web Dashboard (Cloud-Ready Edition)            ")
    print(f"  Dashboard:  http://{args.host}:{args.port}/               ")
    print(f"  API Docs:   http://{args.host}:{args.port}/docs           ")
    print(f"  Edge Ingest: POST http://{args.host}:{args.port}/api/events/ingest")
    print(f"  Hot Reload:  {'ENABLED' if args.reload else 'DISABLED'}   ")
    print(f"  Database:   {os.getenv('DATABASE_MODE', 'auto')} (see startup storage message)")
    print(f"  CV/Torch:    NONE (zero vision dependencies)              ")
    print(f"============================================================")

    uvicorn.run(
        "backend.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
