                    # index newly-arrived files so the next attempt can resolve
                    # them.
                    if unresolved_paths and not (
                        result.get("cancelled") or status_value == "cancelled"
                    ):
                        try:
                            from ..plex_client import trigger_plex_partial_scan

                            scan_results = trigger_plex_partial_scan(
                                plex_url=settings.get("plex_url", ""),
                                plex_token=settings.get("plex_token", ""),
                                unresolved_paths=unresolved_paths,
                                path_mappings=config.path_mappings,
                            )
                            if scan_results:
                                job_manager.add_log(
                                    job_id,