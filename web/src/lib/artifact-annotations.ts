// Annotations are tied to a specific *version* of a specific artifact — identity is
// `(artifactId, versionId)`, never the raw image source. So a regenerated plot (a new
// version of the same artifact) never inherits the previous version's pins, and the
// same bytes (same versionId) keep theirs. `open_artifact` images carry an
// authoritative `artifact_id`/`version_id`; MCP-returned images (not snapshotted
// server-side) fall back to a derived identity so they stay annotatable.

export interface ArtifactImageIdentity {
  key: string; // `${artifactId}::${versionId}` — the annotation map + DB key
  artifactId: string;
  versionId: string;
  title: string;
  source: string; // what the panel renders and read_file ingests (snapshot path/URL)
  name: string; // the original file name (e.g. "plot.png"), for user-facing labels
  versionSeq: number; // this version's 1-based sequence within the artifact (0 if unknown)
}

export interface ArtifactImageAnnotation {
  id: string;
  sequence: number;
  x: number;
  y: number;
  xRatio: number;
  yRatio: number;
  imageWidth: number;
  imageHeight: number;
  comment: string;
  createdAt: string;
  updatedAt: string;
}

export interface ArtifactAnnotationRecord {
  image: ArtifactImageIdentity;
  annotations: ArtifactImageAnnotation[];
  updatedAt: string;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

const ANNOTATION_COORDINATE_GRID_MAXIMUM = 999;

function annotationGridValue(ratio: number): number {
  return Math.min(ANNOTATION_COORDINATE_GRID_MAXIMUM, Math.max(0, Math.round(ratio * ANNOTATION_COORDINATE_GRID_MAXIMUM)));
}

// A stable short hash for deriving a versionId when an artifact carries none (MCP
// images). djb2 over the source is enough — it only needs to be deterministic per
// distinct source so annotations bind to the right image.
function derivedVersionId(source: string): string {
  const material = source.startsWith("data:image/") ? source.slice(0, 256) : source;
  let hash = 5381;
  for (let index = 0; index < material.length; index += 1) {
    hash = ((hash << 5) + hash + material.charCodeAt(index)) >>> 0;
  }
  return hash.toString(16);
}

export function artifactImageKey(artifactId: string, versionId: string): string {
  return `${artifactId}::${versionId}`;
}

export function imageIdentityForArtifact(artifact: Record<string, unknown>): ArtifactImageIdentity | null {
  const kind = (stringValue(artifact.type) || stringValue(artifact.kind)).toLowerCase();
  if (kind !== "image") return null;
  const source = stringValue(artifact.data) || stringValue(artifact.src) || stringValue(artifact.url) || stringValue(artifact.file) || stringValue(artifact.source);
  if (!source) return null;
  const artifactId =
    stringValue(artifact.artifact_id) ||
    stringValue(artifact.artifactId) ||
    stringValue(artifact.id) ||
    "image";
  const versionId =
    stringValue(artifact.version_id) || stringValue(artifact.versionId) || derivedVersionId(source);
  const title = stringValue(artifact.title) || "Image";
  const name = stringValue(artifact.name) || stringValue(artifact.filename);
  const versionSeqRaw = artifact.version_seq ?? artifact.versionSeq;
  const versionSeq = typeof versionSeqRaw === "number" ? versionSeqRaw : Number(versionSeqRaw) || 0;
  return { key: artifactImageKey(artifactId, versionId), artifactId, versionId, title, source, name, versionSeq };
}

export function annotationsPayload(record: ArtifactAnnotationRecord): Record<string, unknown> {
  return {
    kind: "artifact_image_annotations",
    image: {
      artifact_id: record.image.artifactId,
      version_id: record.image.versionId,
      key: record.image.key,
      title: record.image.title,
      name: record.image.name,
      version_seq: record.image.versionSeq,
      // The snapshot path/URL, so the model can read the clean original with read_file
      // and the backend can stamp numbered markers on it.
      source: record.image.source,
    },
    annotations: record.annotations.map((annotation) => ({
      id: annotation.id,
      sequence: annotation.sequence,
      comment: annotation.comment,
      position: {
        x: annotationGridValue(annotation.xRatio),
        y: annotationGridValue(annotation.yRatio),
        x_ratio: annotation.xRatio,
        y_ratio: annotation.yRatio,
      },
      image_size: {
        width: annotation.imageWidth,
        height: annotation.imageHeight,
      },
      created_at: annotation.createdAt,
      updated_at: annotation.updatedAt,
    })),
  };
}
