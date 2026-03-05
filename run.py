import os
import json
import readline
import shutil

import click

import asyncio
import os
import platform
import re
import shutil
import time
import unicodedata

import click
import pyperclip

import salmon.trackers
from salmon import cfg
from salmon.checks import mqa_test
from salmon.checks.integrity import (
    check_integrity,
    format_integrity,
    sanitize_integrity,
)
from salmon.checks.logs import check_log_cambia
from salmon.checks.upconverts import upload_upconvert_test
from salmon.common import commandgroup
from salmon.constants import ENCODINGS, FORMATS, SOURCES, TAG_ENCODINGS
from salmon.converter.downconverting import (
    convert_folder,
    generate_conversion_description,
)
from salmon.converter.transcoding import (
    generate_transcode_description,
    transcode_folder,
)
from salmon.errors import AbortAndDeleteFolder, InvalidMetadataError
from salmon.images import upload_cover
from salmon.tagger import (
    metadata_validator_base,
    validate_encoding,
    validate_source,
)
from salmon.tagger.audio_info import (
    check_hybrid,
    gather_audio_info,
    recompress_path,
)
from salmon.tagger.cover import compress_pictures, download_cover_if_nonexistent
from salmon.tagger.foldername import rename_folder
from salmon.tagger.folderstructure import check_folder_structure
from salmon.tagger.metadata import get_metadata
from salmon.tagger.pre_data import construct_rls_data
from salmon.tagger.retagger import rename_files, tag_files
from salmon.tagger.review import review_metadata
from salmon.tagger.tags import check_tags, gather_tags, standardize_tags
from salmon.uploader import _prompt_source, edit_metadata, execute_downconversion_tasks, last_min_dupe_check, prompt_downconversion_choice, recheck_dupe, upload_and_report
from salmon.uploader.dupe_checker import (
    check_existing_group,
    dupe_check_recent_torrents,
    generate_dupe_check_searchstrs,
    get_search_results,
    print_recent_upload_results,
    print_torrents,
)
from salmon.uploader.preassumptions import print_preassumptions
from salmon.uploader.request_checker import check_requests
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import (
    check_spectrals,
    generate_lossy_approval_comment,
    get_spectrals_path,
    handle_spectrals_upload_and_deletion,
    post_upload_spectral_check,
    report_lossy_master,
)
from salmon.uploader.upload import (
    concat_track_data,
    prepare_and_upload,
)


import salmon.commands
from salmon.common import commandgroup
from salmon.errors import FilterError, LoginError, UploadError
from salmon.release_notification import show_release_notification
from salmon import cfg


# Batch-mode configuration
MODE = "spectrals"  # "spectrals", "check", "upload", "delete", "qobuz"
BASE_PATH = "NEED TO CHANGE (something like /home/user/uploads)"
RESULTS_JSON_PATH = "folder_results.json"
PROWLARR_ZERO_PATH = "prowlarr_zero.json"
SOURCE = "WEB"


def cleanup_tmp_dir():
    """Clean up the temporary directory if configured."""
    if cfg.directory.tmp_dir and cfg.directory.clean_tmp_dir:
        try:
            for item in os.listdir(cfg.directory.tmp_dir):
                item_path = os.path.join(cfg.directory.tmp_dir, item)
                try:
                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        os.unlink(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                except Exception as e:
                    click.secho(f"Failed to remove {item_path}: {e}", fg="yellow")
            click.secho(f"Cleaned temporary directory: {cfg.directory.tmp_dir}", fg="green")
        except Exception as e:
            click.secho(f"Failed to clean temporary directory: {e}", fg="yellow")


def _load_results(path):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except Exception as e:
            click.secho(f"Failed to read results JSON: {e}", fg="yellow")
    return {}


def _save_results(path, results):
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, sort_keys=True)


def _load_json_list(path, label):
    if not os.path.isfile(path):
        click.secho(f"{label} JSON not found: {path}", fg="yellow")
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return data
        click.secho(f"{label} JSON is not a list: {path}", fg="yellow")
    except Exception as e:
        click.secho(f"Failed to read {label} JSON: {e}", fg="yellow")
    return []


def _normalize_text(value):
    if value is None:
        return ""
    text = str(value).lower().replace("&", "and")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_folder_artist_title(folder_name):
    if " - " not in folder_name:
        return None, None, None
    artist_part, rest = folder_name.split(" - ", 1)
    year_match = re.search(r"\((\d{4})\)", rest)
    year = year_match.group(1) if year_match else None
    title_part = re.split(r"\s+\(|\s+\[", rest, 1)[0]
    return artist_part.strip(), title_part.strip(), year


def _matches_artist_title(folder_name, title):
    if not title:
        return False
    title_norm = _normalize_text(title)
    if not title_norm:
        return False
    folder_artist, folder_title, parsed_year = _parse_folder_artist_title(folder_name)
    if folder_artist and folder_title:
        folder_artist_norm = _normalize_text(folder_artist)
        folder_title_norm = _normalize_text(folder_title)
        if not folder_artist_norm or not folder_title_norm:
            return False
        if folder_title_norm != title_norm:
            return False
        return True
    folder_norm = _normalize_text(folder_name)
    return title_norm in folder_norm


def _get_status(entry):
    if not isinstance(entry, dict):
        return None
    if "status" in entry:
        return entry.get("status")
    check_status = entry.get("check")
    if check_status == "exists":
        return "exists"
    upload_status = entry.get("upload")
    if upload_status == "uploaded":
        return "uploaded"
    spectrals_status = entry.get("spectrals")
    if spectrals_status is not None:
        return spectrals_status
    if check_status is not None:
        return check_status
    if upload_status is not None:
        return upload_status
    return None


def _update_results(results, folder_name, full_path, status):
    entry = results.setdefault(folder_name, {})
    entry["path"] = full_path
    entry["status"] = status


def _list_folders(base_path):
    folders = []
    for name in sorted(os.listdir(base_path)):
        full_path = os.path.join(base_path, name)
        if os.path.isdir(full_path):
            folders.append((name, full_path))
    return folders


def _get_audio_format(audio_info):
    if not audio_info:
        return None
    first_file = next(iter(audio_info.keys()))
    _, ext = os.path.splitext(first_file)
    return FORMATS.get(ext.lower())



def up(
    path,
):
    """Command to upload an album folder to a Gazelle Site."""
    gazelle_site = salmon.trackers.get_class('RED')()
    source = 'WEB'
    return upload(
        gazelle_site,
        path,
        source
    )


def upload(
    gazelle_site,
    path,
    source
):
    
    """Upload an album folder to Gazelle Site
    Offer the choice to upload to another tracker after completion."""
    path = os.path.abspath(path)
    remove_downloaded_cover_image = cfg.image.remove_auto_downloaded_cover_image
    audio_info = gather_audio_info(path)
    hybrid = check_hybrid(audio_info)
    standardize_tags(path)
    tags = gather_tags(path)
    rls_data = construct_rls_data(
        tags,
        audio_info,
        source,
        None,
        scene=False,
        hybrid=hybrid,
    )

    try:
        
        # mqa check - done
        click.secho("Checking for MQA release (first file only)", fg="cyan", bold=True)
        mqa_test(path)

        # upconvert test
        if rls_data["encoding"] == "24bit Lossless":
            upload_upconvert_test(path)

        # group selection
        searchstrs = generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])
        if len(searchstrs) > 0:
            group_id = check_existing_group(gazelle_site, searchstrs)

        # spectrals
        spectral_ids = None
        _lossy_master, spectral_ids = check_spectrals(path, audio_info, format=rls_data["format"])

        # metadata
        metadata, new_source_url = get_metadata(path, tags, rls_data)
        if new_source_url is not None:
            source_url = new_source_url
            click.secho(f"New Source URL: {source_url}", fg="yellow")
        path, metadata, tags, audio_info = edit_metadata(
            path, tags, metadata, source, rls_data, False, True, spectral_ids, False
        )

        track_data = concat_track_data(tags, audio_info)
    except click.Abort:
        click.secho("\nAborting upload...", fg="red")
        return "failed"
    except AbortAndDeleteFolder:
        shutil.rmtree(path)
        click.secho("\nDeleted folder, aborting upload...", fg="red")
        return "failed"

    spectrals_path = get_spectrals_path(path)
    spectral_urls = handle_spectrals_upload_and_deletion(spectrals_path, spectral_ids)
    if cfg.upload.requests.last_minute_dupe_check:
        last_min_dupe_check(gazelle_site, searchstrs)

    # Shallow copy to avoid errors on multiple uploads in one session.
    remaining_gazelle_sites = ['RED']
    tracker = gazelle_site.site_code
    torrent_id = None
    cover_url = None
    stored_cover_url = None  # Store the cover URL for reuse across trackers
    # Regenerate searchstrs (will be used to search for requests)
    searchstrs = generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])

    seedbox_uploader = UploadManager()

    remaining_gazelle_sites.remove(tracker)

    # Handle cover image for this tracker
    if group_id:
        if not remove_downloaded_cover_image:
            download_cover_if_nonexistent(path, metadata["cover"])
        # Don't need cover URL for existing groups
        cover_url = None
    else:
        # For new groups, we need a cover URL
        # If we already uploaded it for a previous tracker, reuse that URL
        if not stored_cover_url:
            cover_path, is_downloaded = download_cover_if_nonexistent(path, metadata["cover"])
            stored_cover_url = upload_cover(cover_path)
            if is_downloaded and remove_downloaded_cover_image:
                click.secho("Removing downloaded Cover Image File", fg="yellow")
                os.remove(cover_path)
        cover_url = stored_cover_url

    if cfg.image.auto_compress_cover:
        compress_pictures(path)

    torrent_id, group_id, torrent_path, torrent_content, url = upload_and_report(
        gazelle_site,
        path,
        group_id,
        metadata,
        cover_url,
        track_data,
        hybrid,
        False,
        spectral_urls,
        spectral_ids,
        None,
        None,
        source_url,
        seedbox_uploader,
        source=source,
    )

    request_id = None

    torrent_content.comment = url
    torrent_content.write(torrent_path, overwrite=True)

    print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)

    
    selected_tasks = prompt_downconversion_choice(rls_data, track_data)
    if selected_tasks:
        display_names = [task["name"] for task in selected_tasks]
        click.secho(f"\nSelected formats for downconversion: {', '.join(display_names)}", fg="green", bold=True)

        # Execute downconversion tasks
        execute_downconversion_tasks(
            selected_tasks,
            path,
            gazelle_site,
            group_id,
            metadata,
            cover_url,
            track_data,
            hybrid,
            False,
            spectral_urls,
            spectral_ids,
            None,
            request_id,
            source_url,
            seedbox_uploader,
            source,
            url,
        )

    tracker = None

    seedbox_uploader.execute_upload()
    return "uploaded"



def main():
    try:
        cleanup_tmp_dir()
        # show_release_notification()
        # click.echo()

        # commandgroup(obj={})
        mode = MODE.strip().lower()
        base_path = os.path.abspath(BASE_PATH)
        results_path = os.path.abspath(RESULTS_JSON_PATH)

        results = _load_results(results_path)
        folders = []
        if mode in ("spectrals", "check", "upload", "delete"):
            if not os.path.isdir(base_path):
                click.secho(f"Base path does not exist: {base_path}", fg="red")
                return
            folders = _list_folders(base_path)
            if not folders:
                click.secho(f"No folders found in {base_path}", fg="yellow")
                return

        if mode == "spectrals":
            for folder_name, full_path in folders:
                prior_status = _get_status(results.get(folder_name, {}))
                if prior_status == "exists" or prior_status == "permatranscoded":
                    click.secho(
                        f"\nSpectrals check: {folder_name} (skipping, status: {prior_status})",
                        fg="yellow",
                    )
                    continue
                click.secho(f"\nSpectrals check: {folder_name}", fg="cyan")
                status = "failed"
                try:
                    audio_info = gather_audio_info(full_path, sort_by_tracknumber=True)
                    format_ = _get_audio_format(audio_info)
                    is_24bit = any(t.get("precision") == 24 for t in audio_info.values())
                    if format_ == "FLAC" and is_24bit:
                        try:
                            upload_upconvert_test(full_path)
                        except click.Abort:
                            click.secho(
                                "Upconvert check flagged files; continuing to spectrals.",
                                fg="yellow",
                            )
                    check_spectrals(full_path, audio_info, format=format_ or "UNKNOWN")
                    resp = click.prompt(
                        "Spectrals verdict? [t]ranscoded / [p]ermatranscoded / [l]ossless",
                        default="l",
                    ).strip().lower()
                    if resp.startswith("t"):
                        status = "transcoded"
                    elif resp.startswith("p"):
                        status = "permatranscoded"
                    else:
                        status = "lossless"
                except Exception as e:
                    click.secho(f"Spectrals check failed for {folder_name}: {e}", fg="red")
                    status = "failed"
                _update_results(results, folder_name, full_path, status)
                _save_results(results_path, results)

        elif mode == "check":
            gazelle_site = salmon.trackers.get_class('RED')()
            for folder_name, full_path in folders:
                click.secho(f"\nRED check: {folder_name}", fg="cyan")
                prior_status = _get_status(results.get(folder_name, {}))
                status = "failed"
                should_save = True
                try:
                    audio_info = gather_audio_info(full_path)
                    hybrid = check_hybrid(audio_info)
                    standardize_tags(full_path)
                    tags = gather_tags(full_path)
                    rls_data = construct_rls_data(
                        tags,
                        audio_info,
                        SOURCE,
                        None,
                        scene=False,
                        hybrid=hybrid,
                    )
                    searchstrs = generate_dupe_check_searchstrs(
                        rls_data["artists"],
                        rls_data["title"],
                        rls_data["catno"],
                    )
                    results_on_site = get_search_results(gazelle_site, searchstrs) if searchstrs else []
                    if results_on_site:
                        status = "exists"
                    else:
                        if prior_status in (None, "exists"):
                            status = "does not exist"
                        else:
                            should_save = False
                except Exception as e:
                    click.secho(f"RED check failed for {folder_name}: {e}", fg="red")
                    status = "failed"
                    if prior_status is not None:
                        should_save = False
                if should_save:
                    _update_results(results, folder_name, full_path, status)
                    _save_results(results_path, results)

        elif mode == "upload":
            for folder_name, full_path in folders:
                click.secho(f"\nUploading {folder_name}...", fg="cyan")
                prior = results.get(folder_name, {})
                prior_status = _get_status(prior)
                if prior_status == "exists":
                    click.secho(f"Skipping {folder_name} (status: exists)", fg="yellow")
                    continue
                if prior_status != "lossless":
                    click.secho(f"Skipping {folder_name} (status: {prior_status})", fg="yellow")
                    continue
                try:
                    res = up(full_path)
                    if res in (None, "uploaded"):
                        _update_results(results, folder_name, full_path, "uploaded")
                        _save_results(results_path, results)
                    else:
                        click.secho(
                            f"Upload failed for {folder_name}; status unchanged.",
                            fg="red",
                        )
                except Exception as e:
                    click.secho(f"Upload failed for {folder_name}: {e}", fg="red")
        elif mode == "delete":
            for folder_name, full_path in folders:
                click.secho(f"\nDeleting check: {folder_name}", fg="cyan")
                prior_status = _get_status(results.get(folder_name, {}))
                should_delete = prior_status in ("exists", "permatranscoded", "uploaded")
                if not should_delete:
                    click.secho(f"Skipping {folder_name} (status: {prior_status})", fg="yellow")
                    continue
                try:
                    shutil.rmtree(full_path)
                except Exception as e:
                    click.secho(f"Delete failed for {folder_name}: {e}", fg="red")
        elif mode == "qobuz":
            click.echo("rip --no-db -q 2 url ", nl=False)
            if not results:
                click.secho(f"No results found in {results_path}.", fg="yellow")
                return
            prowlarr_path = os.path.abspath(PROWLARR_ZERO_PATH)
            prowlarr_items = _load_json_list(prowlarr_path, "Prowlarr")
            if not prowlarr_items:
                return
            transcoded = []
            for folder_name, entry in results.items():
                if _get_status(entry) == "transcoded":
                    transcoded.append(folder_name)
            if not transcoded:
                click.secho("No transcoded entries found.", fg="yellow")
                return
            for folder_name in sorted(transcoded):
                _, _, folder_year = _parse_folder_artist_title(folder_name)
                matches = [
                    item
                    for item in prowlarr_items
                    if _matches_artist_title(
                        folder_name,
                        item.get("title"),
                    )
                ]
                if folder_year and matches:
                    year_matches = [
                        m for m in matches if str(m.get("first_release_date", "")).startswith(str(folder_year))
                    ]
                    if year_matches:
                        matches = year_matches
                if len(matches) == 1:
                    
                    click.echo(f"https://play.qobuz.com/album/{matches[0].get('qobuz_id')} ", nl=False)
                elif len(matches) == 0:
                    pass
                    # click.secho(f"{folder_name} -> no match", fg="yellow")
                else:
                    pass
                    # click.secho(f"{folder_name} -> multiple", fg="yellow")
                    # qobuz_ids = [m.get("qobuz_id") for m in matches if m.get("qobuz_id")]
                    # click.secho(
                    #     f"{folder_name} -> multiple matches: {', '.join(qobuz_ids)}",
                    #     fg="yellow",
                    # )
            click.echo()
            
        else:
            click.secho(
                f"Unknown MODE '{MODE}'. Use: spectrals, check, upload, delete, qobuz.",
                fg="red",
            )
            return
        
    except (UploadError, FilterError) as e:
        click.secho(f"There was an error: {e}", fg="red", bold=True)
    except LoginError:
        click.secho(
            "Failed to log in. Is your session cookie up to date? Run the checkconf command to diagnose.", fg="red"
        )
    except ImportError as e:
        click.secho(f"You are missing required dependencies: {e}", fg="red")


if __name__ == "__main__":
    main()
