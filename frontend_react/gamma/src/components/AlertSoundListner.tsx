import { useEffect, useRef } from "react";
import { useAlert } from "../features/hooks/useAlert";
import alertSound from "../assets/audio/alertSound.mp3"

type AlertItem = {
  id: number;
};

export default function AlertSoundListener() {
  const { data } = useAlert();

  const audioRef = useRef<HTMLAudioElement | null>(null);

  const seenAlertIds = useRef<Set<number>>(
    new Set(),
  );

  const firstLoad = useRef(true);

  useEffect(() => {
    audioRef.current = new Audio(alertSound);

    audioRef.current.preload =
      "auto";
  }, []);

  useEffect(() => {
    if (!data?.items?.length) {
      return;
    }

    // Don't play sound for alerts
    // already present when app loads
    if (firstLoad.current) {
      data.items.forEach(
        (alert: AlertItem) => {
          seenAlertIds.current.add(
            alert.id,
          );
        },
      );

      firstLoad.current = false;

      return;
    }

    const newAlerts = data.items.filter(
      (alert: AlertItem) =>
        !seenAlertIds.current.has(
          alert.id,
        ),
    );

    if (newAlerts.length === 0) {
      return;
    }

    newAlerts.forEach(
      (alert: AlertItem) => {
        seenAlertIds.current.add(
          alert.id,
        );
      },
    );

    if (audioRef.current) {
        console.log(
    "🔔 Alert sound triggered",
    newAlerts,
  );
      audioRef.current.currentTime =
        0;

        audioRef.current
        .play()
        .then(() => {
        console.log(
            "✅ Alert sound played successfully"
        );
        })
        .catch((error) => {
          console.error(
            "Alert sound blocked:",
            error,
          );
        });
    }

    console.log(
      "NEW ALERTS:",
      newAlerts,
    );
  }, [data?.items]);

  return null;
}