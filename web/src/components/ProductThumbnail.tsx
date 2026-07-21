import { useEffect, useState } from "react";

interface ProductThumbnailProps {
  /** 商品公开缩略图地址，缺失时渲染不可点击的占位内容。 */
  imageUrl: string;
  /** 图片替代文本和大图预览标题共用商品名称。 */
  productName: string;
  /** 根据承载列表选择固定缩略图尺寸。 */
  size: "desktop" | "mobile";
  /** 用户点击可用缩略图后打开同页大图预览。 */
  onPreview: (imageUrl: string, productName: string) => void;
}

interface ImagePreviewProps {
  /** 当前待查看的图片地址；为空时不渲染预览层。 */
  imageUrl: string | null;
  /** 当前图片对应的商品名称，用于无障碍说明。 */
  productName: string;
  /** 关闭预览层并清空所属页面的选中图片。 */
  onClose: () => void;
}

/** 渲染可降级的商品缩略图，并将有效图片的预览行为交给所属页面。 */
export function ProductThumbnail({ imageUrl, productName, size, onPreview }: ProductThumbnailProps) {
  // 图片资源失效后在当前列表项中保持占位，避免后续重新渲染反复请求。
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    // 筛选或新快照替换图片地址时，允许新地址重新尝试加载。
    setLoadFailed(false);
  }, [imageUrl]);

  if (!imageUrl || loadFailed) {
    return <span className={`product-thumbnail ${size} is-placeholder`} aria-label="暂无商品图片">暂无图片</span>;
  }
  return <button type="button" className={`product-thumbnail ${size}`} onClick={() => onPreview(imageUrl, productName)} aria-label={`查看${productName}大图`}><img src={imageUrl} alt={productName} onError={() => setLoadFailed(true)} /></button>;
}

/** 显示一个可由遮罩、关闭按钮或 Escape 键关闭的商品大图预览。 */
export function ImagePreview({ imageUrl, productName, onClose }: ImagePreviewProps) {
  useEffect(() => {
    // 仅在预览打开期间监听 Escape，关闭后立即解除全局监听。
    if (!imageUrl) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [imageUrl, onClose]);

  if (!imageUrl) return null;
  return <div className="image-preview-overlay" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><section className="image-preview-dialog" role="dialog" aria-modal="true" aria-label={`${productName}大图预览`}><button type="button" className="image-preview-close" onClick={onClose} aria-label="关闭大图预览">×</button><img src={imageUrl} alt={productName} /></section></div>;
}
