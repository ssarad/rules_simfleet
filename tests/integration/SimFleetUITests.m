#import <XCTest/XCTest.h>

@interface SimFleetUITests : XCTestCase
@end

@implementation SimFleetUITests

- (void)exerciseApplication {
  XCUIApplication *application = [[XCUIApplication alloc] init];
  [application launch];
  XCTAssertTrue([application.staticTexts[@"SIMFLEET_READY"] waitForExistenceWithTimeout:15.0]);

  // This makes accidental serial execution visible in CI timing while keeping
  // the integration test short when three method shards run concurrently.
  [NSThread sleepForTimeInterval:1.0];
  [application terminate];
}

- (void)testMethodOne {
  [self exerciseApplication];
}

- (void)testMethodTwo {
  [self exerciseApplication];
}

- (void)testMethodThree {
  [self exerciseApplication];
}

- (void)testMethodFour {
  [self exerciseApplication];
}

- (void)testMethodFive {
  [self exerciseApplication];
}

@end
