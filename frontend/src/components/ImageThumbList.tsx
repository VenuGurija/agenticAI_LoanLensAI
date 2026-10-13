export interface ImageThumb {
  imageId: string;
  filename: string;
  previewUrl: string;
}

interface ImageThumbListProps {
  images: ImageThumb[];
  onRemove: (imageId: string) => void;
  disabled?: boolean;
}

export function ImageThumbList({ images, onRemove, disabled = false }: ImageThumbListProps) {
  if (images.length === 0) {
    return null;
  }

  return (
    <ul className="thumb-list">
      {images.map((image) => (
        <li key={image.imageId} className="thumb-item">
          <img className="thumb-image" src={image.previewUrl} alt={image.filename} />
          <button
            className="thumb-remove"
            type="button"
            disabled={disabled}
            aria-label={`Remove ${image.filename}`}
            onClick={() => onRemove(image.imageId)}
          >
            Remove
          </button>
        </li>
      ))}
    </ul>
  );
}
