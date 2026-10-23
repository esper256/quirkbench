/* Use OSTree's supported archive API with PAX, which preserves security xattrs.
 * The ostree export CLI fixes GNU tar format and loses these attributes.
 * This helper belongs to Bazzite assembly; it is never installed on targets.
 */
#include <archive.h>
#include <ostree.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: export-root REPOSITORY COMMIT OUTPUT\n");
        return 2;
    }
    g_autoptr(GFile) path = g_file_new_for_path(argv[1]);
    g_autoptr(OstreeRepo) repo = ostree_repo_new(path);
    g_autoptr(GFile) root = NULL;
    g_autoptr(GError) error = NULL;
    if (!ostree_repo_open(repo, NULL, &error) ||
        !ostree_repo_read_commit(repo, argv[2], &root, NULL, NULL, &error)) {
        fprintf(stderr, "%s\n", error->message);
        return 1;
    }
    struct archive *output = archive_write_new();
    int status = 1;
    if (archive_write_set_format_pax(output) == ARCHIVE_OK &&
        archive_write_open_filename(output, argv[3]) == ARCHIVE_OK) {
        OstreeRepoExportArchiveOptions options = {0};
        if (ostree_repo_export_tree_to_archive(repo, &options,
                OSTREE_REPO_FILE(root), output, NULL, &error) &&
            archive_write_close(output) == ARCHIVE_OK)
            status = 0;
    }
    if (status)
        fprintf(stderr, "%s\n", error ? error->message : archive_error_string(output));
    archive_write_free(output);
    return status;
}
