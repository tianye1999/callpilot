import XCTest
@testable import CallPilot

final class RootPresentationTests: XCTestCase {
    private let offer = InboundOffer(
        offerId: "offer_abcdefghijkl",
        expiresAt: 9_999
    )

    func testUnpairedPresentationOverridesCallAndOffer() {
        XCTAssertEqual(
            RootPresentation.resolve(
                isPaired: false,
                callState: .inCall(label: "active"),
                incomingOffer: offer
            ),
            .pairing
        )
    }

    func testCallPresentationOverridesIncomingOffer() {
        XCTAssertEqual(
            RootPresentation.resolve(
                isPaired: true,
                callState: .waitingMedia(label: "active"),
                incomingOffer: offer
            ),
            .call
        )
    }

    func testTerminalCallResultRemainsPresentedUntilAcknowledged() {
        for state in [
            CallState.ended(label: "completed", reason: "remote_hangup"),
            CallState.failed(label: "failed", reason: "network", code: "MEDIA_FAILED"),
        ] {
            XCTAssertEqual(
                RootPresentation.resolve(
                    isPaired: true,
                    callState: state,
                    incomingOffer: offer
                ),
                .call
            )
        }
    }

    func testIdlePresentationShowsOfferThenMainWhenOfferClears() {
        XCTAssertEqual(
            RootPresentation.resolve(
                isPaired: true,
                callState: .idle,
                incomingOffer: offer
            ),
            .incomingOffer(offer)
        )
        XCTAssertEqual(
            RootPresentation.resolve(
                isPaired: true,
                callState: .idle,
                incomingOffer: nil
            ),
            .main
        )
    }
}
