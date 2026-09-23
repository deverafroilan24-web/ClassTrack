import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="ClassTrack Web Dashboard - Cloud Deployable")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Server host IP")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument("--reload", action=argparse.BooleanOptionalAction, default=True, help="Enable uvicorn hot reload")
    args = parser.parse_args()

    print(f"============================================================")
    print(f"  ClassTrack Web Dashboard (Cloud-Ready Edition)            ")
    print(f"  Dashboard:  http://{args.host}:{args.port}/               ")
    print(f"  API Docs:   http://{args.host}:{args.port}/docs           ")
    print(f"  Edge Ingest: POST http://{args.host}:{args.port}/api/events/ingest")
    print(f"  Hot Reload:  {'ENABLED' if args.reload else 'DISABLED'}   ")
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
