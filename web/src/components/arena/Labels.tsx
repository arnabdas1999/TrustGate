"use client";

import { useEffect, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { Vector3 } from "three";
import type { Group } from "three";

/**
 * Scene labels without a React root per label: an <Anchor> inside the Canvas projects its world
 * position to screen space every frame into a shared map, and one plain DOM <LabelLayer> over the
 * canvas moves each label to its anchor. Labels can't drop on mount, and stay crisp DOM text.
 */
export type AnchorMap = Map<string, { x: number; y: number; visible: boolean }>;

const v = new Vector3();

export function Anchor({ id, anchors, position }: { id: string; anchors: AnchorMap; position: [number, number, number] }) {
  const ref = useRef<Group>(null);
  useFrame(({ camera, size }) => {
    if (!ref.current) return;
    // matrixWorld is stale until the first render; update it so frame one projects correctly
    ref.current.updateWorldMatrix(true, false);
    v.setFromMatrixPosition(ref.current.matrixWorld).project(camera);
    anchors.set(id, {
      x: ((v.x + 1) / 2) * size.width,
      y: ((1 - v.y) / 2) * size.height,
      visible: v.z < 1,
    });
  });
  useEffect(() => () => void anchors.delete(id), [anchors, id]);
  return <group ref={ref} position={position} />;
}

export interface LabelSpec {
  id: string;
  text: string;
  /** Colour of the leading state dot (omit for no dot). */
  dot?: string;
  className?: string;
}

export function LabelLayer({ anchors, labels }: { anchors: AnchorMap; labels: LabelSpec[] }) {
  const layer = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let frame = 0;
    const tick = () => {
      const root = layer.current;
      if (root) {
        for (const el of root.children as HTMLCollectionOf<HTMLElement>) {
          const a = anchors.get(el.dataset.anchor ?? "");
          if (a && a.visible) {
            el.style.transform = `translate(${a.x}px, ${a.y}px) translate(-50%, -100%)`;
            el.style.opacity = "1";
          } else {
            el.style.opacity = "0";
          }
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [anchors]);

  return (
    <div className="label-layer" ref={layer} aria-hidden>
      {labels.map((l) => (
        <div key={l.id} data-anchor={l.id} className={`tag ${l.className ?? ""}`}>
          {l.dot && <i className="tag-dot" style={{ background: l.dot, boxShadow: `0 0 8px ${l.dot}` }} />}
          <span>{l.text}</span>
        </div>
      ))}
    </div>
  );
}
